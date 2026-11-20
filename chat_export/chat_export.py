import argparse
import base64
import difflib
import html as html_module
import os
import sys
import tempfile
import time
import traceback
import zipfile
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
import re
import shutil
import webbrowser
from pathlib import Path, PureWindowsPath, PurePosixPath
from urllib.parse import quote
from importlib.metadata import version as _pkg_version, PackageNotFoundError

# Attempt to import PyObjC modules for macOS file dialog support
# for this to work, you need to pip install PyObjC
if sys.platform == 'darwin':
    try:
        from AppKit import NSOpenPanel, NSApplication, NSApp
        import objc
        pyobjc_available = True
    except ImportError:
        pyobjc_available = False
else:
    pyobjc_available = False

# Attempt to import pywin32 modules for Windows file dialog support
if sys.platform == 'win32':
    try:
        import win32ui
        import win32con
        pywin32_available = True
    except ImportError:
        pywin32_available = False
else:
    pywin32_available = False


class DateRange:
    def __init__(self, from_date=None, until_date=None):
        self.from_date = from_date
        self.until_date = until_date

        if self.from_date and self.until_date and self.from_date > self.until_date:
            raise ValueError("'From' date must be before 'until' date")

    def contains(self, msg_date):
        """Check if a date falls within this range."""
        if msg_date is None:
            return True  # include messages with unparseable dates
        if self.from_date and msg_date < self.from_date:
            return False
        if self.until_date and msg_date > self.until_date:
            return False
        return True

    def is_filtered(self):
        """Return True if any filtering is applied."""
        return self.from_date is not None or self.until_date is not None

    def format_range(self, date_format):
        """Return a formatted string representation of the range."""
        if not self.is_filtered():
            return None
        from_str = self.from_date.strftime(date_format) if self.from_date else 'start'
        until_str = self.until_date.strftime(date_format) if self.until_date else 'end'
        return f"Filtered: {from_str} to {until_str}"

    @staticmethod
    def parse_date_input(date_str, date_formats):
        """Parse date string in various formats."""
        if not date_str:
            return None

        for fmt in date_formats:
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        raise ValueError("Invalid date format. Please use DD.MM.YYYY, MM/DD/YYYY, DD.MM.YY, MM/DD/YY, DD/MM/YYYY, or DD/MM/YY")



@dataclass(frozen=True)
class Message:
    """Individual message with computed properties using Chat context."""
    id: int
    timestamp: str
    sender: str
    content: str

    # Computed fields (calculated with Chat context)
    attachment_name: Optional[str] = None
    cleaned_content: str = ""
    parsed_date: Optional[datetime] = None
    formatted_timestamp: str = ""
    has_attachment: bool = False

    @classmethod
    def create_with_context(cls, id: int, timestamp: str, sender: str, content: str, chat: 'Chat') -> 'Message':
        """Create a Message with computed properties using Chat context."""
        # Compute attachment name
        attachment_name = cls._extract_attachment_name(content, chat)
        has_attachment = attachment_name is not None

        # Compute cleaned content
        cleaned_content = cls._clean_message_content(content, chat, attachment_name)

        # Compute parsed date
        parsed_date = cls._parse_message_date(timestamp, chat.message_date_format)

        # Compute formatted timestamp
        formatted_timestamp = cls._re_render_with_day_of_week(timestamp, parsed_date)

        return cls(
            id=id,
            timestamp=timestamp,
            sender=sender,
            content=content,
            attachment_name=attachment_name,
            cleaned_content=cleaned_content,
            parsed_date=parsed_date,
            formatted_timestamp=formatted_timestamp,
            has_attachment=has_attachment
        )

    # Attachment patterns as class constants
    ATTACHMENT_PATTERN_ANDROID = r'(.+?\.[a-zA-Z0-9]{0,4}) \(.{1,20} .{1,20}\)'
    # There are two formats for attachment patterns on iOS.
    # The first is the default format: <attached: filename>
    # The second is the new format: <filename eklendi> (e.g. Turkish)
    # Filenames may contain spaces (typical for documents such as "01-My Doc with spaces.pdf").
    # The label is a single word; the filename runs up to the closing '>'.
    ATTACHMENT_PATTERN_IOS = r'<\w{2,20}:\s*([^>\r\n]+)>|<\s*([^>\r\n]+?\.[A-Za-z0-9]{1,8})\s+\w{2,20}>'

    @staticmethod
    def _extract_attachment_name(content: str, chat: 'Chat') -> Optional[str]:
        """Extract attachment filename from message content using various patterns."""
        if chat.is_ios and '<' in content:
            match = re.search(Message.ATTACHMENT_PATTERN_IOS, content)
            if match:
                # pattern contains two formats. If there's no match for the first (group(1) is None), use the second (i.e. Turkish: Filename eklendi)
                result = match.group(1) or match.group(2) if match.groups() else match.group(0)
                result = result.strip()
                if result in chat.attachments_in_zip:
                    return result
        elif not chat.is_ios and '(' in content:
            match = re.search(Message.ATTACHMENT_PATTERN_ANDROID, content)
            if match:
                result = match.group(1) if match.groups() else match.group(0)
                result = result.strip()
                if result in chat.attachments_in_zip:
                    return result
        return None

    @staticmethod
    def _clean_message_content(content: str, chat: 'Chat', attachment_name: Optional[str]) -> str:
        """Remove attachment markers from message content."""
        cleaned_content = content

        # Remove attachment patterns if media present and attachment found
        if chat.has_media and chat.is_ios and '<' in content and attachment_name is not None:
            cleaned_content = re.sub(Message.ATTACHMENT_PATTERN_IOS, '', cleaned_content)
        elif chat.has_media and not chat.is_ios and '(' in content and attachment_name is not None:
            cleaned_content = re.sub(Message.ATTACHMENT_PATTERN_ANDROID, '', cleaned_content)

        if cleaned_content != content:
            # Clean up any remaining file-size annotations like "(3 KB)" or "(1,2 MB)"
            cleaned_content = re.sub(r'\s*\(\d[\d,.]*\s*.{1,10}\)\s*$', '', cleaned_content)

        # Make '<medien ausgeschlossen>' visible in html
        cleaned_content = cleaned_content.replace('<', '[').replace('>', ']')
        cleaned_content = Message._wrap_urls_with_anchor_tags(cleaned_content)
        cleaned_content = cleaned_content.replace(chat.newline_marker, '<br>')

        # Handle call attempts (old exports contained "null", newer contain empty messages "")
        # we don't have any details on calls. Whether if they were video or audio. 
        # Nor if they where attempts or established
        # Nor if they were incoming or outgoing.
        # Just a string "null"
        if cleaned_content == "null" or (cleaned_content == "" and attachment_name is None):
            cleaned_content = "[call (attempt)]"

        return cleaned_content.strip()

    @staticmethod
    def _parse_message_date(timestamp: str, message_date_format: str) -> Optional[datetime]:
        """Parse the date from a message timestamp."""
        try:
            # Remove time part and any AM/PM indicator
            date_str = re.split(', | ', timestamp.replace('[',''))[0]
            return datetime.strptime(date_str, message_date_format).date()
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _re_render_with_day_of_week(timestamp: str, parsed_date: Optional[datetime]) -> str:
        """Parse the date string and re-render it including the day of week."""
        try:
            if parsed_date:
                day_of_week = parsed_date.strftime('%a')
                return f"{day_of_week}, {timestamp}"
            return timestamp
        except (ValueError, AttributeError):
            return timestamp

    @staticmethod
    def _wrap_urls_with_anchor_tags(text: str) -> str:
        """Wrap URLs in anchor tags."""
        url_pattern = re.compile(r'(https?://[^\s]+)')
        return url_pattern.sub(r'<a href="\1" target="_blank">\1</a>', text)


@dataclass
class Chat:
    """Container for chat metadata and messages."""
    # Chat-level metadata
    name: str
    is_ios: bool
    has_media: bool
    attachments_in_zip: frozenset
    message_date_format: str
    newline_marker: str

    # Chat data
    messages: list[Message] = field(default_factory=list)
    senders: list[str] = field(default_factory=list)
    date_range: Optional['DateRange'] = None
    sender_color_map: dict = field(default_factory=dict)
    own_name: str = ""


class Renderer:
    """Base renderer class for message rendering."""

    def __init__(self, output_dir=None):
        self.output_dir = output_dir

    def render(self, chat):
        """Render a Chat object. To be implemented by subclasses."""
        raise NotImplementedError("Subclasses must implement render method")

    def get_generated_files(self) -> list[Path]:
        """Get the generated files."""
        raise NotImplementedError("Subclasses must implement get_generated_files method")


def macos_file_picker():
    """Present a native macOS file dialog to select a file.
    pip install pyobjc-framework-Cocoa
    for this to work
    """
    # Initialize NSApplication if it hasn't been
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # NSApplicationActivationPolicyRegular
    
    # Create and configure the panel first
    panel = NSOpenPanel.alloc().init()
    panel.setCanChooseFiles_(True)
    panel.setCanChooseDirectories_(False)
    panel.setAllowsMultipleSelection_(False)
    panel.setTitle_("Select WhatsApp Chat Export ZIP File")
    panel.setPrompt_("Open")
    
    # Set file type filter to only show .zip files
    panel.setAllowedFileTypes_(["zip"])
    
    # Bring app and panel to front
    app.activateIgnoringOtherApps_(True)
    
    # Run the panel
    response = panel.runModal()
    
    # Clean up
    app.setActivationPolicy_(0)  # NSApplicationActivationPolicyRegular
    
    if response == 1:  # NSModalResponseOK
        urls = panel.URLs()
        if urls and len(urls):
            return str(urls[0].path())
    return None

def windows_file_picker():
    """Use the native Windows file picker with pywin32.
    pip install pywin32
    for this to work.
    """
    # Define file filter format: "Description|*.extension|"
    file_filter = "ZIP Files (*.zip)|*.zip|All Files (*.*)|*.*|"

    dlg = win32ui.CreateFileDialog(1, None, None, 0, file_filter)  # Open dialog (1)
    dlg.SetOFNTitle("Select WhatsApp Chat Export ZIP File")
    dlg.SetOFNInitialDir(os.path.expanduser("~"))  # Start in user's home directory

    if dlg.DoModal() == 1:  # If the user selects a file
        return dlg.GetPathName()

    return None

VERSION = "1.1.2"

try:
    __version__ = _pkg_version("chat-export") or VERSION
except PackageNotFoundError:
    __version__ = VERSION

donate_link = "https://donate.stripe.com/3cI8wO0yD8Wt0ItbV06J204"

def parse_path(path_str: str) -> Path:
    """
    Parse a path string safely, handling both Windows and Unix styles.
    Returns a pathlib.Path object normalized to the current OS.
    """
    # Strip quotes from the path string if present
    path_str = path_str.strip('"\'')
    

    # Decide whether it's Windows or Unix style by looking for a drive letter
    if ":" in path_str[:3] or "\\" in path_str:
        # Looks like Windows
        pure = PureWindowsPath(path_str)
    else:
        # Looks like Unix/Posix
        pure = PurePosixPath(path_str)

    # Convert into an OS-specific Path (resolves separators automatically)
    return Path(pure)

def parse_arguments():
    """Parse command line arguments for both interactive and non-interactive modes."""
    parser = argparse.ArgumentParser(
        description='Convert WhatsApp chat exports to HTML format',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Interactive mode (default)
  python main.py

  # Non-interactive mode
  python main.py -n -z "chat.zip" -p "John Doe"

  # With date filtering
  python main.py -n -z "chat.zip" -p "John Doe" --from-date "01.01.2024" --until-date "31.12.2024"

  # With custom output directory (creates /tmp/chat/ instead of ./chat/)
  python main.py -n -z "chat.zip" -p "John Doe" -o "/tmp"
        '''
    )

    parser.add_argument('-n', '--non-interactive',
                       action='store_true',
                       help='Run in non-interactive mode (requires -z and -p)')

    parser.add_argument('-z', '--zip-file',
                       type=str,
                       help='Path to WhatsApp chat export ZIP file (required in non-interactive mode)')

    parser.add_argument('-p', '--participant',
                       type=str,
                       help='Name of the participant that represents yourself (required in non-interactive mode)')

    parser.add_argument('--from-date',
                       type=str,
                       help='Start date for filtering messages (optional, formats: DD.MM.YYYY, MM/DD/YYYY, DD.MM.YY, MM/DD/YY, DD/MM/YYYY, DD/MM/YY)')

    parser.add_argument('--until-date',
                       type=str,
                       help='End date for filtering messages (optional, formats: DD.MM.YYYY, MM/DD/YYYY, DD.MM.YY, MM/DD/YY, DD/MM/YYYY, DD/MM/YY)')

    parser.add_argument('-o', '--output-dir',
                       type=str,
                       help='Base directory where the ZIP-derived output folder will be created (default: current working directory)')

    parser.add_argument('--embed-media',
                       action='store_true',
                       help='Embed media files as base64 in HTML instead of linking to external files')

    args = parser.parse_args()

    # Validate non-interactive mode requirements
    if args.non_interactive:
        if not args.zip_file:
            parser.error("Non-interactive mode requires --zip-file (-z)")
        if not args.participant:
            parser.error("Non-interactive mode requires --participant (-p)")

    return args


class MessageParser:
    """Handles parsing of WhatsApp chat content into Message objects."""

    # Arabic-Indic and Eastern Arabic-Indic digits plus the Arabic comma, mapped to
    # their ASCII equivalents so timestamps can be matched and parsed with strptime.
    ARABIC_TRANSLATION_TABLE = str.maketrans({
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
        "،": ",",
    })

    # Unicode bidirectional control characters (LRM, RLM, isolates, embeddings,
    # overrides). WhatsApp inserts these around timestamps and names in RTL
    # exports, which breaks line matching, so they are removed from the whole
    # chat text before parsing.
    BIDI_CONTROL_TABLE = {
        ord(ch): None
        for ch in "\u200E\u200F\u202A\u202B\u202C\u202D\u202E\u2066\u2067\u2068\u2069"
    }

    def __init__(self, is_ios=False, has_media=False, attachments_in_zip=None):
        self.is_ios = is_ios
        self.has_media = has_media
        self.attachments_in_zip = attachments_in_zip or set()

        # Chat patterns for different platforms
        # Time separator can be ':' (most locales, e.g. 18:00) or '.' (Indonesian
        # WhatsApp exports, e.g. 18.00), so match either.
        self.chat_patterns = {
            'ios': re.compile(r'\[(\d{1,4}.\d{1,2}.\d{2,4},? \d{1,2}[.:]\d{2}(?:[.:]\d{2})?(?:\s*(?:[AaPp][Mm]|[صم]))?)\] (.*?): (.*)'),
            'android': re.compile(
                r'(\d{1,4}.\d{1,2}.\d{2,4},? \d{1,2}[.:]\d{2}(?:[.:]\d{2})?(?:\s*(?:[AaPp]\.?\s*[Mm]\.?|[صم]))?) - (.*?): (.*)')
        }
        self.whatsapp_patterns = {
            'ios': re.compile(r'\[(\d{1,4}.\d{1,2}.\d{2,4},? \d{1,2}[.:]\d{2}(?:[.:]\d{2})?(?:\s*(?:[AaPp][Mm]|[صم]))?)\] (.*)'),
            'android': re.compile(
                r'(\d{1,4}.\d{1,2}.\d{2,4},? \d{1,2}[.:]\d{2}(?:[.:]\d{2})?(?:\s*(?:[AaPp]\.?\s*[Mm]\.?|[صم]))?) - (.*)')
        }

        self.newline_marker = ' $NEWLINE$ '
        self.message_date_format = "%d.%m.%y"

        self.date_formats = [
            "%d.%m.%Y",  # German format: DD.MM.YYYY
            "%m/%d/%Y",  # US format: MM/DD/YYYY
            "%d.%m.%y",  # German format: DD.MM.YY
            "%m/%d/%y",  # US format: MM/DD/YY
            "%d/%m/%Y",  # Indonesian format: DD/MM/YYYY
            "%d/%m/%y"   # Indonesian format: DD/MM/YY
        ]

    @classmethod
    def normalize_locale_markers(cls, text: str) -> str:
        """Normalize locale markers that interfere with WhatsApp line parsing.

        Converts Arabic digits and the Arabic comma to ASCII and removes all
        bidirectional control characters. This is applied to the whole chat
        text (timestamps, sender names and message bodies alike) and is
        idempotent, so it is safe to call on already normalized content.
        """
        text = text.translate(cls.ARABIC_TRANSLATION_TABLE)
        return text.translate(cls.BIDI_CONTROL_TABLE)

    def get_date_format(self, chat_content):
        """Determine the date format used in the chat."""
        chat_content = self.normalize_locale_markers(chat_content)
        first_line = None
        pattern = self.chat_patterns['ios'] if self.is_ios else self.chat_patterns['android']
        for line in chat_content.split('\n'):
            if pattern.match(line):
                first_line = line
                break

        if first_line is None:
            first_line_content = chat_content.split('\n')[0]
            raise ValueError(f"Could not determine the date format of the chat: {first_line_content}")

        # Use the same splitting logic as elsewhere (comma+space OR plain space)
        # instead of a plain ',' split, since some locales (e.g. Indonesian
        # exports: '30/08/25 18.00 - ...') have no comma between date and time.
        first_line_date = re.split(', | ', first_line.replace('[', ''))[0]
        # find first non-digit in the date string
        deliminator = None
        for char in first_line_date:
            if not char.isdigit():
                deliminator = char
                break
        if deliminator is None:
            raise ValueError(f"Could not find a date separator in '{first_line_date}'. Not a valid WhatsApp date format.")

        # year might be in position 0 or 2, i.e. 2018-12-22 vs 22.12.18 vs 22.12.2018
        if len(first_line_date.split(deliminator)[0]) == 4:
            return f'%Y{deliminator}%m{deliminator}%d'
        # year is in position 2
        # check if year is 2 or 4 digits
        year_pattern = '%y' if len(first_line_date.split(deliminator)[2]) == 2 else '%Y'
        # need to find out if month or day comes first.
        day_before_month = True
        for line in chat_content.split('\n'):
            if not pattern.match(line):
                continue
            date_str = re.split(', | ', line.replace('[',''))[0]
            first, second, _ = date_str.split(deliminator)
            # convert to int
            first = int(first)
            second = int(second)
            if first > 12:
                day_before_month = True
                break
            if second > 12:
                day_before_month = False
                break

        if day_before_month:
            return f'%d{deliminator}%m{deliminator}{year_pattern}'
        else:
            return f'%m{deliminator}%d{deliminator}{year_pattern}'

    def _parse_timestamp_date(self, timestamp):
        """Helper method to parse timestamp for date filtering during message processing."""
        try:
            # Remove time part and any AM/PM indicator
            date_str = re.split(', | ', timestamp.replace('[',''))[0]
            return datetime.strptime(date_str, self.message_date_format).date()
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def trim_zero_widths(text: str) -> str:
        """Trim harmless zero-width characters from the start and end of a string.

        Safe to strip: ZWSP, ZWNJ, ZWJ, BOM.
        Directional controls (LRM, RLM, isolates, embeddings, overrides) never
        reach this point: normalize_locale_markers removes them from the whole
        chat text before parsing.
        """

        SAFE_ZERO_WIDTHS = "\u200B\u200C\u200D\uFEFF"  # ZWSP, ZWNJ, ZWJ, BOM
        return text.strip(SAFE_ZERO_WIDTHS).strip()


    @staticmethod
    def mark_invisible_chars(text: str) -> str:
        """Replace zero-width Unicode characters inside a sender name with ASCII markers.

        This makes otherwise invisible characters visible in the participant
        list so users can match names exactly. Directional controls are not
        handled here because normalize_locale_markers removes them before
        parsing.
        """

        replacements = {
            "\u200B": ":ZWSP:",   # Zero Width Space
            "\u200C": ":ZWNJ:",   # Zero Width Non-Joiner
            "\u200D": ":ZWJ:",    # Zero Width Joiner
            "\uFEFF": ":BOM:",    # Zero Width No-Break Space / BOM
        }

        return "".join(replacements.get(ch, ch) for ch in text)

    def get_senders(self, chat_content):
        """Extract all unique senders from chat content."""
        chat_content = self.normalize_locale_markers(chat_content)
        senders = set()
        pattern = self.chat_patterns['ios'] if self.is_ios else self.chat_patterns['android']
        for line in chat_content.split('\n'):
            match = pattern.match(line)
            if match:
                sender = match.group(2)
                sender = self.trim_zero_widths(sender)
                sender = self.mark_invisible_chars(sender)
                senders.add(sender)
        return sorted(list(senders))

    def _generate_color_map(self, senders, own_name):
        """Generate color mapping for senders."""
        sender_colors = {
            'own': '#d9fdd3',    # WhatsApp green for own messages
            'default': '#ffffff', # White for the second sender
            'whatsapp': '#20c063',
            # Additional colors for other senders
            'others': [
                '#f0e6ff',  # Light purple
                '#fff3e6',  # Light orange
                '#e6fff0',  # Light mint
                '#ffe6e6',  # Light pink
                '#e6f3ff',  # Light blue
                '#fff0f0',  # Lighter pink
                '#e6ffe6',  # Lighter mint
                '#f2e6ff',  # Lighter purple
                '#fff5e6',  # Peach
                '#e6ffff',  # Light cyan
                '#ffe6f0',  # Rose
                '#f0ffe6',  # Light lime
                '#e6e6ff',  # Lavender
                '#ffe6cc',  # Light apricot
                '#e6fff9'   # Light turquoise
            ]
        }

        color_map = {}

        # Remove own name from senders list for color assignment
        other_senders = [s for s in senders if s != own_name]

        # Assign colors to senders
        color_map[own_name] = sender_colors['own']

        # assign whatsapp color
        color_map['WhatsApp'] = sender_colors['whatsapp']

        # Assign white to the first other sender
        if other_senders:
            color_map[other_senders[0]] = sender_colors['default']

        # Assign different colors to remaining senders
        for i, sender in enumerate(other_senders[1:]):
            color_index = i % len(sender_colors['others'])
            color_map[sender] = sender_colors['others'][color_index]

        return color_map

    def _join_message_lines(self, lines):
        """Join the header line of a message with its continuation lines.

        Trailing blank lines are dropped: WhatsApp trims messages on send, so
        they never end with an empty line. Such lines come from the export
        itself, typically the newline that terminates the chat file, and would
        otherwise render as a spurious line break. Blank lines inside a
        message are kept.
        """
        end = len(lines)
        while end > 1 and not lines[end - 1].strip():
            end -= 1
        return lines[0] + ''.join(self.newline_marker + line for line in lines[1:end])

    def parse_messages(self, chat_content, chat_name="", date_range=None, own_name=""):
        """Parse chat content into a Chat object."""
        chat_content = self.normalize_locale_markers(chat_content)

        # Set the message date format
        self.message_date_format = self.get_date_format(chat_content)

        # Create Chat object with metadata
        chat = Chat(
            name=chat_name,
            is_ios=self.is_ios,
            has_media=self.has_media,
            attachments_in_zip=frozenset(self.attachments_in_zip),
            message_date_format=self.message_date_format,
            newline_marker=self.newline_marker,
            messages=[],
            senders=self.get_senders(chat_content),
            date_range=date_range,
            sender_color_map={},
            own_name=own_name
        )

        # Preprocess the chat content to handle multi-line messages
        processed_content = []
        current_line = []
        filtered_count = 0
        total_count = 0

        for line in chat_content.split('\n'):
            pattern = self.chat_patterns['ios'] if self.is_ios else self.chat_patterns['android']
            wapattern = self.whatsapp_patterns['ios'] if self.is_ios else self.whatsapp_patterns['android']
            match = pattern.match(line)
            wamatch = wapattern.match(line)

            if match or wamatch:
                total_count += 1
                if current_line:
                    processed_content.append(self._join_message_lines(current_line))

                # Only add messages within date range
                if match:
                    timestamp = match.group(1)
                    if date_range and not date_range.contains(self._parse_timestamp_date(timestamp)):
                        current_line = []
                        continue
                else:
                    timestamp = wamatch.group(1)
                    if date_range and not date_range.contains(self._parse_timestamp_date(timestamp)):
                        current_line = []
                        continue

                filtered_count += 1
                current_line = [line]
            else:
                if current_line:
                    current_line.append(line)

        # Don't forget to add the last message
        if current_line:
            processed_content.append(self._join_message_lines(current_line))

        # Parse each processed line into Message objects
        messages = []
        for line in processed_content:
            pattern = self.chat_patterns['ios'] if self.is_ios else self.chat_patterns['android']
            wapattern = self.whatsapp_patterns['ios'] if self.is_ios else self.whatsapp_patterns['android']
            match = pattern.match(line)
            wamatch = wapattern.match(line)

            if match:
                timestamp, sender, content = match.groups()
                sender = self.trim_zero_widths(sender)
                sender = self.mark_invisible_chars(sender)
                messages.append(Message.create_with_context(
                    id=len(messages)+1,
                    timestamp=timestamp,
                    sender=sender,
                    content=content,
                    chat=chat
                ))
            elif wamatch:
                timestamp, content = wamatch.groups()
                messages.append(Message.create_with_context(
                    id=len(messages)+1,
                    timestamp=timestamp,
                    sender="WhatsApp",
                    content=content,
                    chat=chat
                ))

        # Update chat with messages and color map
        chat.messages.extend(messages)
        chat.sender_color_map.update(self._generate_color_map(chat.senders, own_name))

        return chat, filtered_count, total_count


class HTMLRenderer(Renderer):
    """Renders messages to HTML format."""

    def __init__(self, output_dir, has_media=False, embed_media=False, zip_path=None, media_path="./media"):
        super().__init__(output_dir)
        self.has_media = has_media
        self.embed_media = embed_media
        self.zip_path = zip_path
        self.media_path = media_path
        self.html_filename = 'chat.html'
        self.html_filename_media_linked = 'chat_media_linked.html'
        if embed_media:
            # replace .zip with .html
            self.html_filename = Path(self.zip_path).stem + '.html'
            self.html_filename_media_linked = None
        self.attachments_to_extract = set()

    def get_generated_files(self) -> list[Path]:
        """Get the generated files."""
        result = [Path(self.output_dir, self.html_filename)]
        if self.html_filename_media_linked:
            result.append(Path(self.output_dir, self.html_filename_media_linked))
        return result

    def get_css_styles(self):
        """Return the CSS styles for the HTML output."""
        return """:root {
            color-scheme: light;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: #172126;
            background: #e9edef;
            --wa-header: #f7f9fa;
            --muted: #667781;
            --line: rgba(35, 53, 61, .1);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            min-height: 100vh;
            background-color: #e9edef;
            background-image: radial-gradient(rgba(91, 112, 120, .11) .7px, transparent .7px);
            background-size: 18px 18px;
            font-size: 15px;
            line-height: 1.45;
        }
        button { font: inherit; }
        .chat-header {
            position: fixed;
            z-index: 20;
            inset: 0 0 auto;
            height: 68px;
            background: rgba(247, 249, 250, .88);
            border-bottom: 1px solid var(--line);
            box-shadow: 0 3px 16px rgba(24, 42, 49, .06);
            -webkit-backdrop-filter: blur(18px) saturate(1.3);
            backdrop-filter: blur(18px) saturate(1.3);
        }
        .chat-header-inner {
            width: min(100%, 960px);
            height: 100%;
            margin: 0 auto;
            padding: 0 20px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
        }
        .chat-header h1 {
            min-width: 0;
            margin: 0;
            overflow: hidden;
            color: #152126;
            font-size: 1.08rem;
            font-weight: 650;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .starred-toggle, .panel-close {
            position: relative;
            display: inline-grid;
            flex: 0 0 auto;
            width: 42px;
            height: 42px;
            place-items: center;
            color: #33464d;
            background: rgba(255, 255, 255, .72);
            border: 1px solid var(--line);
            border-radius: 50%;
            cursor: pointer;
            transition: background .16s ease, transform .16s ease, color .16s ease;
        }
        .starred-toggle:hover, .panel-close:hover { background: #fff; transform: translateY(-1px); }
        .starred-toggle[aria-expanded="true"] { color: #b77a00; }
        .starred-toggle svg, .panel-close svg, .message-star svg { width: 20px; height: 20px; }
        .star-count {
            position: absolute;
            top: -3px;
            right: -4px;
            min-width: 18px;
            height: 18px;
            padding: 0 4px;
            display: grid;
            place-items: center;
            color: #fff;
            background: #00a884;
            border: 2px solid var(--wa-header);
            border-radius: 999px;
            font-size: 10px;
            font-weight: 700;
        }
        .star-count[data-count="0"] { display: none; }
        .chat-container {
            width: min(100%, 960px);
            min-height: 100vh;
            margin: 0 auto;
            padding: 88px 20px 36px;
        }
        .chat-container > p { color: var(--muted); }
        .message {
            position: relative;
            display: flow-root;
            width: fit-content;
            max-width: min(74%, 700px);
            margin: 8px 0;
            padding: 9px 12px 7px;
            border: 1px solid rgba(28, 48, 55, .045);
            border-radius: 9px 9px 9px 3px;
            box-shadow: 0 1px 1px rgba(20, 38, 44, .08), 0 2px 5px rgba(20, 38, 44, .035);
            transition: box-shadow .16s ease, outline-color .16s ease;
        }
        .message.sent {
            margin-right: 0;
            margin-left: auto;
            border-radius: 9px 9px 3px 9px;
        }
        .message.received { margin-right: auto; margin-left: 0; }
        .message.whatsapp {
            width: 100%;
            max-width: 100%;
            margin-right: 0;
            margin-left: 0;
            border-radius: 9px;
        }
        .message.is-selected { box-shadow: 0 2px 12px rgba(20, 38, 44, .16); }
        .message.is-highlighted {
            outline: 2px solid rgba(0, 168, 132, .75);
            outline-offset: 3px;
            animation: message-arrive 1.4s ease-out;
        }
        @keyframes message-arrive {
            0% { outline-color: rgba(0, 168, 132, .9); }
            100% { outline-color: rgba(0, 168, 132, 0); }
        }
        .message-star {
            position: absolute;
            z-index: 2;
            top: 50%;
            display: grid;
            width: 34px;
            height: 34px;
            padding: 0;
            place-items: center;
            color: #77878d;
            background: rgba(255, 255, 255, .92);
            border: 1px solid rgba(35, 53, 61, .12);
            border-radius: 50%;
            box-shadow: 0 3px 10px rgba(20, 38, 44, .14);
            cursor: pointer;
            opacity: 0;
            pointer-events: none;
            transform: translateY(-50%) scale(.92);
            transition: opacity .14s ease, transform .14s ease, color .14s ease;
        }
        .message.received .message-star { right: -42px; }
        .message.sent .message-star { left: -42px; }
        .message:hover .message-star, .message.is-selected .message-star,
        .message:focus-within .message-star { opacity: 1; pointer-events: auto; transform: translateY(-50%) scale(1); }
        .message-star:hover { color: #b77a00; transform: translateY(-50%) scale(1.08) !important; }
        .message-star[aria-pressed="true"] { color: #d99a00; opacity: 1; pointer-events: auto; }
        .message-star[aria-pressed="true"] svg { fill: currentColor; }
        .media {
            max-width: 100%;
            border-radius: 7px;
            margin: 5px 0;
        }
        .text-file {
            border: 1px solid #dce2e4;
            border-radius: 8px;
            padding: 10px;
            background-color: rgba(255, 255, 255, .8);
            margin: 5px 0;
        }
        .text-file h4 { margin: 0 0 10px; color: #33464d; font-size: .9em; }
        .text-content {
            max-height: 300px;
            overflow-y: auto;
            padding: 10px;
            color: #293a40;
            background-color: #fff;
            border: 1px solid #e0e5e6;
            border-radius: 6px;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: .8em;
            white-space: pre-wrap;
            overflow-wrap: anywhere;
        }
        .timestamp {
            float: right;
            margin: 7px 0 0 12px;
            color: var(--muted);
            font-size: .7em;
            white-space: nowrap;
        }
        .sender {
            display: block;
            margin-bottom: 3px;
            color: #147c73;
            font-size: .78em;
            font-weight: 650;
        }
        .content { overflow-wrap: anywhere; }
        .clearfix::after { content: ""; display: table; clear: both; }
        a { color: #087e76; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .starred-backdrop {
            position: fixed;
            z-index: 30;
            inset: 0;
            background: rgba(21, 34, 39, .18);
            -webkit-backdrop-filter: blur(2px);
            backdrop-filter: blur(2px);
        }
        .starred-backdrop[hidden], .starred-panel[hidden] { display: none; }
        .starred-panel {
            position: fixed;
            z-index: 31;
            inset: 12px 12px 12px auto;
            display: flex;
            flex-direction: column;
            width: min(390px, calc(100vw - 24px));
            overflow: hidden;
            color: #172126;
            background: rgba(247, 250, 251, .78);
            border: 1px solid rgba(255, 255, 255, .72);
            border-radius: 22px;
            box-shadow: 0 24px 70px rgba(15, 32, 39, .23), inset 0 1px rgba(255, 255, 255, .8);
            -webkit-backdrop-filter: blur(28px) saturate(1.4);
            backdrop-filter: blur(28px) saturate(1.4);
            animation: panel-in .18s ease-out;
        }
        @keyframes panel-in { from { opacity: 0; transform: translateX(12px); } to { opacity: 1; transform: translateX(0); } }
        .starred-panel-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 19px 18px 15px;
            border-bottom: 1px solid rgba(35, 53, 61, .09);
        }
        .starred-panel-title { margin: 0; font-size: 1.05rem; font-weight: 680; }
        .starred-panel-subtitle { margin: 3px 0 0; color: var(--muted); font-size: .82rem; }
        .panel-close { width: 36px; height: 36px; }
        .starred-list { flex: 1; overflow-y: auto; padding: 10px; }
        .starred-empty {
            margin: 0;
            padding: 34px 18px;
            color: var(--muted);
            text-align: center;
            font-size: .92rem;
        }
        .starred-item {
            display: block;
            width: 100%;
            margin: 0 0 7px;
            padding: 12px 13px;
            color: inherit;
            text-align: left;
            background: rgba(255, 255, 255, .65);
            border: 1px solid rgba(255, 255, 255, .72);
            border-radius: 13px;
            cursor: pointer;
            transition: background .14s ease, transform .14s ease;
        }
        .starred-item:hover { background: rgba(255, 255, 255, .96); transform: translateY(-1px); }
        .starred-item-meta { display: flex; justify-content: space-between; gap: 10px; color: #147c73; font-size: .77rem; font-weight: 650; }
        .starred-item-time { color: var(--muted); font-size: .72rem; font-weight: 400; white-space: nowrap; }
        .starred-item-preview { display: -webkit-box; margin-top: 5px; overflow: hidden; color: #314249; font-size: .88rem; line-height: 1.4; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
        @media (max-width: 600px) {
            .chat-header-inner { padding: 0 14px; }
            .chat-container { padding: 82px 14px 26px; }
            .message { max-width: 82%; }
            .starred-panel { inset: 8px 8px 8px auto; width: min(390px, calc(100vw - 16px)); border-radius: 18px; }
        }
        @media (hover: none) {
            .message-star { opacity: 0; pointer-events: none; }
            .message.is-selected .message-star,
            .message-star[aria-pressed="true"] { opacity: 1; pointer-events: auto; }
        }
        @media print {
            body { background: #fff; }
            .chat-header, .starred-panel, .starred-backdrop, .message-star { display: none !important; }
            .chat-container { width: 100%; min-height: 0; padding: 12px 0; }
            .message { box-shadow: none; break-inside: avoid; }
        }"""

    def get_html_header(self):
        """Generate the HTML header."""
        safe_name = html_module.escape(self.chat.name)
        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{safe_name}</title>
    <style>
        {self.get_css_styles()}
    </style>
</head>
<body>
<header class="chat-header">
    <div class="chat-header-inner">
        <h1 title="{safe_name}">{safe_name}</h1>
        <button class="starred-toggle" id="starred-toggle" type="button" aria-label="Open starred messages" aria-expanded="false" aria-controls="starred-panel" title="Starred messages">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3Z"/></svg>
            <span class="star-count" id="star-count" data-count="0" aria-live="polite">0</span>
        </button>
    </div>
</header>
<div class="starred-backdrop" id="starred-backdrop" hidden></div>
<aside class="starred-panel" id="starred-panel" aria-label="Starred messages" hidden>
    <div class="starred-panel-header">
        <div><h2 class="starred-panel-title">Starred messages</h2><p class="starred-panel-subtitle" id="starred-panel-subtitle">Saved for this chat</p></div>
        <button class="panel-close" id="starred-close" type="button" aria-label="Close starred messages" title="Close">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>
        </button>
    </div>
    <div class="starred-list" id="starred-list"><p class="starred-empty">Messages you star will show up here.</p></div>
</aside>
<main class="chat-container" id="chat-messages">"""

    def get_html_footer(self):
        """Generate the HTML footer."""
        return """
 </main>
<script>
(() => {
    const messages = Array.from(document.querySelectorAll('.message[data-id]'));
    const toggle = document.getElementById('starred-toggle');
    const panel = document.getElementById('starred-panel');
    const backdrop = document.getElementById('starred-backdrop');
    const closeButton = document.getElementById('starred-close');
    const list = document.getElementById('starred-list');
    const count = document.getElementById('star-count');
    const subtitle = document.getElementById('starred-panel-subtitle');
    const chatPath = location.pathname.replace(/\/(?:chat|chat_media_linked)\.html$/, '');
    const storageKey = `chat-export:starred:${chatPath}:${document.title}`;
    let starredIds = new Set();

    try {
        const saved = JSON.parse(localStorage.getItem(storageKey) || '[]');
        if (Array.isArray(saved)) {
            const idsInChat = new Set(messages.map(message => message.dataset.id));
            starredIds = new Set(saved.map(String).filter(id => idsInChat.has(id)));
        }
    } catch (_) {
        // Starring still works for this page view when browser storage is unavailable.
    }

    function persist() {
        try { localStorage.setItem(storageKey, JSON.stringify(Array.from(starredIds))); }
        catch (_) { /* Keep the current-page state even if storage is blocked. */ }
    }

    function updateStarButton(message) {
        const button = message.querySelector('.message-star');
        if (!button) return;
        const isStarred = starredIds.has(message.dataset.id);
        button.setAttribute('aria-pressed', String(isStarred));
        button.setAttribute('aria-label', `${isStarred ? 'Unstar' : 'Star'} message ${message.dataset.id}`);
        button.title = isStarred ? 'Remove star' : 'Star message';
    }

    function renderList() {
        count.textContent = String(starredIds.size);
        count.dataset.count = String(starredIds.size);
        subtitle.textContent = starredIds.size === 1 ? '1 saved message' : `${starredIds.size} saved messages`;
        list.replaceChildren();
        const savedMessages = messages.filter(message => starredIds.has(message.dataset.id));
        if (!savedMessages.length) {
            const empty = document.createElement('p');
            empty.className = 'starred-empty';
            empty.textContent = 'Messages you star will show up here.';
            list.append(empty);
            return;
        }
        for (const message of savedMessages) {
            const item = document.createElement('button');
            item.type = 'button';
            item.className = 'starred-item';
            item.dataset.targetId = message.dataset.id;
            const meta = document.createElement('span');
            meta.className = 'starred-item-meta';
            const sender = document.createElement('span');
            sender.textContent = message.querySelector('.sender')?.textContent || 'Message';
            const time = document.createElement('span');
            time.className = 'starred-item-time';
            time.textContent = (message.querySelector('.timestamp')?.textContent || '').replace(/\s*\(#\d+\)$/, '');
            const preview = document.createElement('span');
            preview.className = 'starred-item-preview';
            const content = (message.querySelector('.content')?.textContent || '').replace(/\s+/g, ' ').trim();
            preview.textContent = content || 'Media message';
            meta.append(sender, time);
            item.append(meta, preview);
            list.append(item);
        }
    }

    function setPanelOpen(isOpen) {
        panel.hidden = !isOpen;
        backdrop.hidden = !isOpen;
        toggle.setAttribute('aria-expanded', String(isOpen));
        toggle.setAttribute('aria-label', isOpen ? 'Close starred messages' : 'Open starred messages');
        if (isOpen) closeButton.focus();
        else toggle.focus();
    }

    messages.forEach(updateStarButton);
    renderList();

    document.addEventListener('click', event => {
        const starButton = event.target.closest('.message-star');
        if (starButton) {
            event.stopPropagation();
            const message = starButton.closest('.message');
            const id = message?.dataset.id;
            if (!id) return;
            if (starredIds.has(id)) starredIds.delete(id);
            else starredIds.add(id);
            updateStarButton(message);
            persist();
            renderList();
            return;
        }

        const listItem = event.target.closest('.starred-item');
        if (listItem) {
            const target = messages.find(message => message.dataset.id === listItem.dataset.targetId);
            if (target) {
                setPanelOpen(false);
                target.scrollIntoView({ behavior: 'smooth', block: 'center' });
                target.setAttribute('tabindex', '-1');
                target.focus({ preventScroll: true });
                target.classList.add('is-highlighted');
                window.setTimeout(() => target.classList.remove('is-highlighted'), 1500);
            }
            return;
        }

        const message = event.target.closest('.message');
        if (message) {
            messages.forEach(item => item.classList.toggle('is-selected', item === message));
        } else if (!event.target.closest('.starred-panel')) {
            messages.forEach(item => item.classList.remove('is-selected'));
        }
    });

    toggle.addEventListener('click', () => setPanelOpen(panel.hidden));
    closeButton.addEventListener('click', () => setPanelOpen(false));
    backdrop.addEventListener('click', () => setPanelOpen(false));
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && !panel.hidden) setPanelOpen(false);
    });
})();
</script>
</body>
</html>"""

    def get_mime_type(self, filename):
        """Get MIME type based on file extension."""
        ext = filename.lower()
        if ext.endswith(('.jpg', '.jpeg')):
            return 'image/jpeg'
        elif ext.endswith('.png'):
            return 'image/png'
        elif ext.endswith('.gif'):
            return 'image/gif'
        elif ext.endswith('.webp'):
            return 'image/webp'
        elif ext.endswith('.mp4'):
            return 'video/mp4'
        elif ext.endswith('.opus'):
            return 'audio/ogg'
        elif ext.endswith('.mp3'):
            return 'audio/mpeg'
        elif ext.endswith('.wav'):
            return 'audio/wav'
        elif ext.endswith('.m4a'):
            return 'audio/mp4'
        elif ext.endswith('.pdf'):
            return 'application/pdf'
        elif ext.endswith(('.doc', '.docx')):
            return 'application/msword'
        elif ext.endswith(('.xls', '.xlsx')):
            return 'application/vnd.ms-excel'
        elif ext.endswith(('.ppt', '.pptx')):
            return 'application/vnd.ms-powerpoint'
        elif ext.endswith('.txt'):
            return 'text/plain'
        elif ext.endswith('.rtf'):
            return 'application/rtf'
        elif ext.endswith('.zip'):
            return 'application/zip'
        elif ext.endswith('.rar'):
            return 'application/x-rar-compressed'
        elif ext.endswith('.7z'):
            return 'application/x-7z-compressed'
        elif ext.endswith('.tar'):
            return 'application/x-tar'
        elif ext.endswith('.gz'):
            return 'application/gzip'
        elif ext.endswith('.csv'):
            return 'text/csv'
        elif ext.endswith('.json'):
            return 'application/json'
        elif ext.endswith('.xml'):
            return 'application/xml'
        elif ext.endswith('.html'):
            return 'text/html'
        elif ext.endswith('.css'):
            return 'text/css'
        elif ext.endswith('.js'):
            return 'application/javascript'
        elif ext.endswith('.py'):
            return 'text/x-python'
        elif ext.endswith('.java'):
            return 'text/x-java-source'
        elif ext.endswith(('.cpp', '.c', '.h')):
            return 'text/x-c'
        else:
            return 'application/octet-stream'

    def encode_media_to_base64(self, attachment_name):
        """Read media file from zip and encode to base64."""
        try:
            with zipfile.ZipFile(self.zip_path, 'r') as zip_ref:
                with zip_ref.open(attachment_name) as media_file:
                    media_data = media_file.read()
                    base64_data = base64.b64encode(media_data).decode('utf-8')
                    mime_type = self.get_mime_type(attachment_name)
                    return f"data:{mime_type};base64,{base64_data}"
        except Exception as e:
            print(f"Warning: Could not encode {attachment_name} to base64: {e}")
            return None

    def media_file_url(self, attachment_name):
        """Build a media URL, percent-encoding spaces and other reserved characters."""
        return f"{self.media_path}/{quote(attachment_name, safe='/')}"

    def render_media_element(self, attachment_name, is_media_linked=False):
        """Render a media element based on its file extension."""
        media_path = self.media_file_url(attachment_name)

        if is_media_linked:
            # Always show as link in media-linked version
            return f'<a href="{media_path}">📎 {attachment_name}</a><br>'

        # Render media inline in main version
        ext = attachment_name.lower()
        
        # If embed_media is enabled, try to encode as base64
        if self.embed_media and self.zip_path:
            base64_data = self.encode_media_to_base64(attachment_name)
            if base64_data:
                # Images
                if ext.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
                    return f'<img class="media" src="{base64_data}"><br>'
                # Videos
                elif ext.endswith('.mp4'):
                    return f'<video class="media" controls><source src="{base64_data}" type="video/mp4"></video><br>'
                # Audio files
                elif ext.endswith('.opus'):
                    return f'<audio class="media" controls><source src="{base64_data}" type="audio/ogg"></audio><br>'
                elif ext.endswith('.wav'):
                    return f'<audio class="media" controls><source src="{base64_data}" type="audio/wav"></audio><br>'
                elif ext.endswith('.mp3'):
                    return f'<audio class="media" controls><source src="{base64_data}" type="audio/mpeg"></audio><br>'
                elif ext.endswith('.m4a'):
                    return f'<audio class="media" controls><source src="{base64_data}" type="audio/mp4"></audio><br>'
                
                # Other documents - provide download link with base64 data
                else:
                    return f'<a href="{base64_data}" download="{attachment_name}">📎 {attachment_name}</a><br>'
        
        # Fallback to file references
        if ext.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
            return f'<img class="media" src="{media_path}"><br>'
        elif ext.endswith('.mp4'):
            return f'<video class="media" controls><source src="{media_path}" type="video/mp4"></video><br>'
        elif ext.endswith('.opus'):
            return f'<audio class="media" controls><source src="{media_path}" type="audio/ogg"></audio><br>'
        elif ext.endswith('.wav'):
            return f'<audio class="media" controls><source src="{media_path}" type="audio/wav"></source></audio><br>'
        elif ext.endswith('.mp3'):
            return f'<audio class="media" controls><source src="{media_path}" type="audio/mpeg"></audio><br>'
        elif ext.endswith('.m4a'):
            return f'<audio class="media" controls><source src="{media_path}" type="audio/mp4"></audio><br>'
        else:
            return f'<a href="{media_path}">📎 {attachment_name}</a><br>'

    def render_message(self, message, sender_color_map, own_name, main_f, media_f):
        """Render a single message to both file handles."""
        # Determine message alignment and background color
        is_own_message = message.sender == own_name
        is_wa_message = message.sender == "WhatsApp"
        message_class = "sent" if is_own_message else "received"
        if is_wa_message:
            message_class = 'whatsapp'
        bg_color = sender_color_map.get(message.sender, '#ffffff')

        # Common message structure
        message_start = f'\n<div class="message {message_class} clearfix" data-id="{message.id}" style="background-color: {bg_color};">'
        star_button = (
            '<button class="message-star" type="button" aria-pressed="false" '
            f'aria-label="Star message {message.id}" title="Star message">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3Z"/>'
            '</svg></button>'
        )
        sender_div = f'<div class="sender">{html_module.escape(message.sender)}</div>'
        content_start = '<div class="content">'
        content_end = '</div>'
        timestamp_span = f'<span class="timestamp">{message.formatted_timestamp} (#{message.id})</span>'

        message_end = '</div>'

        # Write message start to both files
        main_f.write(message_start)
        media_f.write(message_start)

        # Add a per-message control for starring in the interactive HTML.
        main_f.write(star_button)
        media_f.write(star_button)

        # Write sender to both files
        main_f.write(sender_div)
        media_f.write(sender_div)

        # Write content start to both files
        main_f.write(content_start)
        media_f.write(content_start)

        # Check if the message contains media
        if message.has_attachment:
            attachment_name = message.attachment_name
            self.attachments_to_extract.add(attachment_name)

            # Render media differently for each file
            main_f.write(self.render_media_element(attachment_name, is_media_linked=False))
            media_f.write(self.render_media_element(attachment_name, is_media_linked=True))

        # Add the message content to both files
        cleaned_content = message.cleaned_content
        if cleaned_content:
            main_f.write(f'{cleaned_content}')
            media_f.write(f'{cleaned_content}')

        # Write content end, timestamp, and message end to both files
        main_f.write(content_end)
        media_f.write(content_end)
        main_f.write(timestamp_span)
        media_f.write(timestamp_span)
        main_f.write(message_end)
        media_f.write(message_end)

    def render(self, chat):
        """Render chat to HTML files."""
        print("Writing HTML files...")

        self.chat = chat
        # Prepare file paths
        main_html_path = os.path.join(self.output_dir, self.html_filename)
        if self.html_filename_media_linked:
            media_linked_html_path = os.path.join(self.output_dir, self.html_filename_media_linked)
        else:
            # Use a proper temp file instead of a hardcoded name
            tmp_fd, media_linked_html_path = tempfile.mkstemp(suffix='.tmp', prefix='chat_export_')
            os.close(tmp_fd)

        try:
            # Open both files for writing
            with open(main_html_path, 'w', encoding='utf-8') as main_f, \
                 open(media_linked_html_path, 'w', encoding='utf-8') as media_f:

                # Write header to both files
                header = self.get_html_header()
                main_f.write(header)
                media_f.write(header)

                # Write date range and attribution to both files
                if chat.date_range and chat.date_range.is_filtered():
                    date_range_str = chat.date_range.format_range(chat.message_date_format)
                    if date_range_str:
                        date_html = f'<p style="color: #667781;">{date_range_str}</p>'
                        main_f.write(date_html)
                        media_f.write(date_html)

                attribution = '<p style="color: #667781;">This rendering has been created with the free offline tool `chat-export` from https://chat-export.click </p>'
                main_f.write(attribution)
                media_f.write(attribution)

                message_count = 0

                for message in chat.messages:
                    self.render_message(message, chat.sender_color_map, chat.own_name, main_f, media_f)
                    message_count += 1

                # Write footer to both files
                footer = self.get_html_footer()
                main_f.write(footer)
                media_f.write(footer)
        finally:
            # Clean up temp file if embed_media mode (no media-linked HTML needed)
            if not self.html_filename_media_linked and os.path.exists(media_linked_html_path):
                os.remove(media_linked_html_path)

        return self.attachments_to_extract


class ChatExport:
    def __init__(self, zip_path, from_date=None, until_date=None, participant_name=None, base_output_dir=None, embed_media=False):
        # Validate zip file existence
        if not os.path.exists(zip_path):
            raise FileNotFoundError(f"Could not find the file: {zip_path}\nPlease check if the file path is correct.")

        if not zip_path.lower().endswith('.zip'):
            raise ValueError(f"The file {zip_path} is not a zip file.\nPlease provide a valid WhatsApp chat export zip file.")

        self.zip_path = zip_path

        # Pre-set values for non-interactive mode
        self.from_date = from_date
        self.until_date = until_date
        self.participant_name = participant_name
        self.embed_media = embed_media

        # Set up output directory: base_output_dir/zip_filename or just zip_filename
        zip_stem = Path(zip_path).stem
        if self.embed_media:
            if base_output_dir:
                self.output_dir = parse_path(base_output_dir)
            else:
                self.output_dir = Path("")
        else: 
            if base_output_dir:
                # Normalize the base output directory path (handle Windows paths, quotes, etc.)
                normalized_base_dir = parse_path(base_output_dir)
                self.output_dir = Path(os.path.join(normalized_base_dir, zip_stem))
            else:
                self.output_dir = Path(zip_stem)
        self.media_dir = os.path.join(self.output_dir, "media")

        self.own_name = participant_name
        self.attachments_in_zip = set()
        self.has_media = False
        self.is_ios = False

        self.date_formats = [
            "%d.%m.%Y",  # German format: DD.MM.YYYY
            "%m/%d/%Y",  # US format: MM/DD/YYYY
            "%d.%m.%y",  # German format: DD.MM.YY
            "%m/%d/%y",  # US format: MM/DD/YY
            "%d/%m/%Y",  # Indonesian format: DD/MM/YYYY
            "%d/%m/%y"   # Indonesian format: DD/MM/YY
        ]

        # Initialize modular components (will be set up later after platform detection)
        self.parser = None
        self.renderer = None

    def validate_participant(self, participant_name, senders):
        """Validate that the specified participant exists in the chat.
        If not found, display all participants and raise an error."""
        if participant_name not in senders:
            print(f"\nError: Participant '{participant_name}' not found in the chat.")
            print("\nFound the following participants in the chat:")
            for i, sender in enumerate(senders, 1):
                print(f"{i}. {sender}")
            print("\nPlease use one of the names listed above exactly as shown.")
            raise ValueError(f"Participant '{participant_name}' not found in chat participants ({', '.join(senders)}). Make sure to use one of the listed names.")
        return True

    def parse_date_input(self, date_str):
        """Parse date string in either US or German format."""
        return DateRange.parse_date_input(date_str, self.date_formats)

    def setup_modular_components(self):
        """Initialize the MessageParser and HTMLRenderer components."""
        # Setup parser
        self.parser = MessageParser(
            is_ios=self.is_ios,
            has_media=self.has_media,
            attachments_in_zip=self.attachments_in_zip
        )
        

        # Setup renderer
        self.renderer = HTMLRenderer(
            output_dir=self.output_dir,
            has_media=self.has_media,
            embed_media=self.embed_media,
            zip_path=self.zip_path
        )

    @staticmethod
    def most_similar(target: str, candidates: list[str]) -> str:
        """Return the string from candidates most similar to target."""
        return max(candidates, key=lambda c: difflib.SequenceMatcher(None, target, c).ratio())

    # Files/dirs this tool writes into a non-embed output folder.
    _EXPORT_DIR_ENTRIES = frozenset({"chat.html", "chat_media_linked.html", "media"})
    _IGNORABLE_DIR_ENTRIES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})

    @staticmethod
    def _is_protected_path(path: Path) -> bool:
        """True for filesystem roots, the user's home, or the current working directory."""
        try:
            resolved = Path(path).resolve()
        except OSError:
            return True
        if resolved == resolved.parent:
            return True
        for special in (Path.home(), Path.cwd()):
            try:
                if resolved == special.resolve():
                    return True
            except OSError:
                continue
        return False

    def _is_replaceable_export_dir(self, output_dir: Path, zip_stem: str) -> bool:
        """True only if this is a previous chat-export folder we may delete.

        Requires an exact directory name match (not a suffix), refuses protected
        paths, and refuses folders that contain anything other than our output files.
        """
        if self.embed_media:
            return False
        path = Path(output_dir)
        if not path.exists() or not path.is_dir():
            return False
        if path.name != zip_stem:
            return False
        if self._is_protected_path(path):
            return False
        try:
            entries = {p.name for p in path.iterdir()}
        except OSError:
            return False
        return (entries - self._IGNORABLE_DIR_ENTRIES) <= self._EXPORT_DIR_ENTRIES

    def _prepare_output_directories(self):
        """Create output dirs. Delete a previous export only when that is clearly safe."""
        zip_stem = Path(self.zip_path).stem
        output_path = Path(self.output_dir)

        if output_path.exists() and not self.embed_media:
            if self._is_replaceable_export_dir(output_path, zip_stem):
                print(f"Cleaning existing directory: {self.output_dir}")
                shutil.rmtree(self.output_dir)
            elif output_path.is_file() or self._is_protected_path(output_path) or any(output_path.iterdir()):
                raise ValueError(
                    f"Output directory '{output_path}' already exists and is not a previous "
                    f"chat-export (expected only chat.html, chat_media_linked.html, and media/). "
                    f"Refusing to delete it. Use --output-dir to choose another location, "
                    f"or remove the directory manually."
                )

        os.makedirs(self.output_dir, exist_ok=True)
        if self.has_media and not self.embed_media:
            os.makedirs(self.media_dir, exist_ok=True)

    def _read_chat_from_zip(self) -> str:
        """Validate the ZIP and read the chat text. Does not touch the output directory."""
        zip_base_name = Path(self.zip_path).stem
        try:
            with zipfile.ZipFile(self.zip_path, 'r') as zip_ref:
                chat_file_candidates = [f for f in zip_ref.namelist() if f.lower().endswith('.txt')]
                if not chat_file_candidates:
                    raise FileNotFoundError("No .txt file found in the ZIP archive. Not a valid WhatsApp export zip.")
                if '_chat.txt' in chat_file_candidates:
                    self.is_ios = True
                    chat_file = '_chat.txt'
                else:
                    self.is_ios = False
                    chat_file = self.most_similar(f"{zip_base_name}.txt", chat_file_candidates)

                for file in zip_ref.namelist():
                    if file != chat_file:
                        self.attachments_in_zip.add(file)
                        self.has_media = True

                if chat_file not in zip_ref.namelist():
                    raise FileNotFoundError(f"The chat file '{chat_file}' does not exist in the ZIP archive. Not a valid WhatsApp export zip.")

                with zip_ref.open(chat_file) as f:
                    chat_content = f.read().decode('utf-8')
        except zipfile.BadZipFile:
            raise ValueError(f"The file {self.zip_path} is not a valid ZIP file.")

        kind = 'iOS' if self.is_ios else 'Android'
        if self.has_media:
            print(f"ZIP file is an {kind} export with media/attachments, '{chat_file}' is the chat text file.")
        else:
            print(f"ZIP file is an {kind} export without media/attachments, '{chat_file}' is the chat text file.")
        return chat_content

    def process_chat(self):
        # Ask for optional date range
        print("\nOptional: Enter date range to filter messages")
        print("Supported formats: MM/DD/YYYY, DD.MM.YYYY, MM/DD/YY, DD.MM.YY")
        print("Leave empty to skip")
        while True:
            try:
                from_date_str = input("From date (optional): ").strip()
                self.from_date = self.parse_date_input(from_date_str)
                break
            except ValueError as e:
                print(f"Error: {e}")
                if input("Try again? [Y/n]: ").lower() == 'n':
                    break

        while True:
            try:
                until_date_str = input("Until date (optional): ").strip()
                self.until_date = self.parse_date_input(until_date_str)
                if self.from_date and self.until_date and self.from_date > self.until_date:
                    raise ValueError("'From' date must be before 'until' date")
                break
            except ValueError as e:
                print(f"Error: {e}")
                if input("Try again? [Y/n]: ").lower() == 'n':
                    break
        

        chat_content = self._read_chat_from_zip()
        self.setup_modular_components()

        # Create date range for filtering
        date_range = DateRange(self.from_date, self.until_date) if (self.from_date or self.until_date) else None

        # Get list of senders first to let user choose their name
        senders = self.parser.get_senders(chat_content)
        print("\nFound the following participants in the chat:")
        for i, sender in enumerate(senders, 1):
            print(f"{i}. {sender}")

        while True:
            try:
                choice = int(input("\nEnter the number corresponding to your name: ")) - 1
                if 0 <= choice < len(senders):
                    self.own_name = senders[choice]
                    break
                else:
                    print("Invalid choice. Please try again.")
            except ValueError:
                print("Please enter a valid number.")
        
        processing_start_time = time.time()
        
        # Parse messages using the new MessageParser - now returns a Chat object
        chat, filtered_count, total_count = self.parser.parse_messages(
            chat_content,
            chat_name=Path(self.zip_path).stem,
            date_range=date_range,
            own_name=self.own_name
        )

        if date_range and date_range.is_filtered():
            print(f"\n{filtered_count} of {total_count} messages match date range filter.")
            if filtered_count == 0:
                raise ValueError("No messages found in the specified date range. Aborting.")
        print(f"Exporting {len(chat.messages)} messages.")

        self._prepare_output_directories()

        # Render messages using the new HTMLRenderer
        attachments_to_extract = self.renderer.render(chat)

        if self.has_media and not self.embed_media:
            print("Extracting attachments/media...")
            # extract attachments of rendered messages
            with zipfile.ZipFile(self.zip_path, 'r') as zip_ref:
                # Extract media files
                for file in zip_ref.namelist():
                    if file in attachments_to_extract:
                        zip_ref.extract(file, self.media_dir)
        elif self.has_media and self.embed_media:
            print("Media will be embedded as base64 in HTML (no file extraction needed)")
        processing_end_time = time.time()
        print(f"Processing took {processing_end_time - processing_start_time:.3f} seconds")

    def process_chat_non_interactive(self):
        """Process chat in non-interactive mode using pre-set parameters."""
        # Validate date parameters if provided
        if self.from_date:
            try:
                self.from_date = self.parse_date_input(self.from_date)
            except ValueError as e:
                raise ValueError(f"Invalid from-date format: {e}")

        if self.until_date:
            try:
                self.until_date = self.parse_date_input(self.until_date)
            except ValueError as e:
                raise ValueError(f"Invalid until-date format: {e}")

        if self.from_date and self.until_date and self.from_date > self.until_date:
            raise ValueError("'From' date must be before 'until' date")

        processing_start_time = time.time()

        chat_content = self._read_chat_from_zip()
        self.setup_modular_components()

        # Create date range for filtering
        date_range = DateRange(self.from_date, self.until_date) if (self.from_date or self.until_date) else None
        print(f"from date: {self.from_date}, until date: {self.until_date}")

        # Get list of senders and validate the provided participant
        senders = self.parser.get_senders(chat_content)
        self.validate_participant(self.own_name, senders)

        # Parse messages using the new MessageParser - now returns a Chat object
        chat, filtered_count, total_count = self.parser.parse_messages(
            chat_content,
            chat_name=Path(self.zip_path).stem,
            date_range=date_range,
            own_name=self.own_name
        )

        if date_range and date_range.is_filtered():
            print(f"\n{filtered_count} of {total_count} messages match date range filter.")
            if filtered_count == 0:
                raise ValueError("No messages found in the specified date range. Aborting.")
        print(f"Exporting {len(chat.messages)} messages.")

        self._prepare_output_directories()

        # Render messages using the new HTMLRenderer
        attachments_to_extract = self.renderer.render(chat)

        if self.has_media and not self.embed_media:
            print("Extracting attachments/media...")
            # extract attachments of rendered messages
            with zipfile.ZipFile(self.zip_path, 'r') as zip_ref:
                # Extract media files
                for file in zip_ref.namelist():
                    if file in attachments_to_extract:
                        zip_ref.extract(file, self.media_dir)
        elif self.has_media and self.embed_media:
            print("Media will be embedded as base64 in HTML (no file extraction needed)")
        processing_end_time = time.time()
        print(f"Processing took {processing_end_time - processing_start_time:.3f} seconds")
        return chat
        


def check_tkinter_availability():
    """Check if tkinter is available and working on the system."""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        print("Tkinter is not available on your system. Using prompt input instead of file picker dialog.")
        return False

def browse_zip_file():
    if sys.platform == 'darwin' and pyobjc_available:
        result = macos_file_picker()
        return result
    elif sys.platform == 'win32' and pywin32_available:
        result = windows_file_picker()
        return result

    # Check tkinter availability first
    if not check_tkinter_availability():
        # Fallback to command line input
        file_path = input("Please enter the path to your WhatsApp chat export ZIP file: ").strip()
        return file_path if file_path else None

    import tkinter as tk
    from tkinter import filedialog
    # Initialize Tkinter root window and hide it
    root = tk.Tk()
    root.withdraw()  # Hide the main window

    # Open file dialog and set file type filter to .zip files
    zip_file_path = filedialog.askopenfilename(
        title="Select a WhatsApp Chat Export ZIP file",
        filetypes=[("ZIP files", "*.zip")]
    )

    # Return the selected file path
    return zip_file_path

def open_html_file_in_browser(html_file: Path):
    """Opens the specified HTML file in the default web browser."""
    # Get the absolute path of the file
    file_path = Path(os.path.abspath(html_file))
    # Open the file in the default web browser
    # file:///
    webbrowser.open(f"file://{file_path.as_posix()}")

def main():
    args = parse_arguments()
    if args.non_interactive:
        # Non-interactive mode
        print(f"chat-export v{__version__} - Non-interactive mode")
        print("----------------------------------------")
        success = False
        try:
            print(f"Processing file: {args.zip_file}...")

            # Parse dates if provided
            from_date = args.from_date if args.from_date else None
            until_date = args.until_date if args.until_date else None

            chat_export = ChatExport(args.zip_file, from_date, until_date, args.participant, args.output_dir, args.embed_media)
            chat_export.process_chat_non_interactive()
            print(f'Written: {", ".join([str(p.absolute()) for p in chat_export.renderer.get_generated_files()])}')
            print("Done.")
            success = True

        except FileNotFoundError as e:
            print(f"Error: {e}")
            sys.exit(1)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            print(traceback.format_exc())
            sys.exit(1) 

    else:
        # Interactive mode (original behavior)
        print(f"Welcome to chat-export v{__version__}")
        print("----------------------------------------")
        print("Select the WhatsApp chat export ZIP file you want to convert to HTML.")
        success = False
        try:
            selected_zip_file = browse_zip_file()
            if not selected_zip_file:
                raise FileNotFoundError("No file selected.")
            print(f"Processing selected file: {selected_zip_file}...")
            chat_export = ChatExport(selected_zip_file, base_output_dir=args.output_dir, embed_media=args.embed_media)
            chat_export.process_chat()
            print(f'Written: {", ".join([str(p.absolute()) for p in chat_export.renderer.get_generated_files()])}')
            print("Done.")
            open_in_browser = input("Would you like to open them in the browser? [Y/n]: ").strip().lower()
            if open_in_browser != 'n':
                for file in reversed(chat_export.renderer.get_generated_files()):
                    open_html_file_in_browser(file.absolute())
            success = True

        except FileNotFoundError as e:
            print(f"Error: {e}")
        except ValueError as e:
            print(f"Error: {e}")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            print(traceback.format_exc())

        if success and input("Do you like the tool and want to buy me a coffee? [y/N]: ").strip().lower() == 'y':
            webbrowser.open(donate_link)
        if not success:
            print("Press enter to exit")
            input()

if __name__ == "__main__":
    main()
