"""SPIKE-03 bounded byte policy; not an upload API or object authorization service."""
from dataclasses import dataclass
from hashlib import sha256

MAX_BYTES = 10 * 1024 * 1024
MAX_RECEIPTS = 10
SCAN_TAG = 'GuardDutyMalwareScanStatus'


class FileRejected(ValueError):
    def __init__(self, code='invalid_file'):
        self.code = code
        super().__init__('File processing rejected.')


@dataclass(frozen=True)
class ByteIdentity:
    size: int
    sha256: str
    detected_type: str


def read_bounded(stream):
    result = bytearray()
    while len(result) <= MAX_BYTES:
        block = stream.read(min(65536, MAX_BYTES + 1 - len(result)))
        if not block:
            break
        result.extend(block)
    if not result or len(result) > MAX_BYTES:
        raise FileRejected('size_limit')
    return bytes(result)


def inspect_bytes(data, purpose, declared_type):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        raise FileRejected('size_limit')
    if purpose not in {'knowledge', 'receipt'}:
        raise FileRejected('purpose')
    if data.startswith(b'%PDF-'):
        detected = 'application/pdf'
    elif data.startswith(b'\x89PNG\r\n\x1a\n'):
        detected = 'image/png'
    elif data.startswith(b'\xff\xd8\xff'):
        detected = 'image/jpeg'
    else:
        try:
            text = data.decode('utf-8', errors='strict')
        except UnicodeError:
            raise FileRejected('type_mismatch') from None
        if any(ord(c) < 32 and c not in '\n\r\t' for c in text) or '\x7f' in text:
            raise FileRejected('type_mismatch')
        detected = 'text/markdown' if declared_type == 'text/markdown' else 'text/plain'
    allowed = {'application/pdf','image/png','image/jpeg'} if purpose == 'receipt' else {'application/pdf','text/plain','text/markdown'}
    if declared_type != detected or detected not in allowed:
        raise FileRejected('type_mismatch')
    return ByteIdentity(len(data), sha256(data).hexdigest(), detected)


def require_same_bytes(data, identity):
    if len(data) != identity.size or sha256(data).hexdigest() != identity.sha256:
        raise FileRejected('integrity_mismatch')


def scan_ready(tags):
    """Only the independently protected provider tag can unlock a candidate."""
    if not isinstance(tags, list) or sum(t.get('Key') == SCAN_TAG for t in tags) != 1:
        return False
    return any(t.get('Key') == SCAN_TAG and t.get('Value') == 'NO_THREATS_FOUND' for t in tags)


def receipt_count(count):
    if type(count) is not int or not 1 <= count <= MAX_RECEIPTS:
        raise FileRejected('count_limit')
