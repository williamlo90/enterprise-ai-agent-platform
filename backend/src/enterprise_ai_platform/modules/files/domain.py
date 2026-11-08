from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

MAX_FILE_BYTES = 10 * 1024 * 1024
MEDIA = frozenset({'application/pdf','image/jpeg','image/png','text/plain','text/markdown'})


class FileFailure(RuntimeError):
    def __init__(self,status=503):
        self.status=status
        super().__init__('File operation rejected.')


def uuid_text(value):
    try:
        if not isinstance(value,str) or str(UUID(value))!=value:raise ValueError()
    except (ValueError,TypeError,AttributeError):raise FileFailure(400) from None
    return value


@dataclass(frozen=True,repr=False)
class Upload:
    purpose: str
    mime: str
    data: bytes
    key: str
    collection_id: str | None = None

    def __post_init__(self):
        uuid_text(self.key)
        if self.purpose not in {'knowledge','receipt'} or self.mime not in MEDIA:raise FileFailure(400)
        if self.collection_id is not None:uuid_text(self.collection_id)
        if self.purpose=='receipt' and self.collection_id is not None:raise FileFailure(400)
        if type(self.data) is not bytes or not 0<len(self.data)<=MAX_FILE_BYTES:raise FileFailure(413)
        if self.data.startswith(b'%PDF-'):detected='application/pdf'
        elif self.data.startswith(b'\x89PNG\r\n\x1a\n'):detected='image/png'
        elif self.data.startswith(b'\xff\xd8\xff'):detected='image/jpeg'
        else:
            try:text=self.data.decode('utf-8',errors='strict')
            except UnicodeError:raise FileFailure(422) from None
            if any(ord(c)<32 and c not in '\n\r\t' for c in text) or '\x7f' in text:raise FileFailure(422)
            detected='text/markdown' if self.mime=='text/markdown' else 'text/plain'
        allowed={'application/pdf','image/jpeg','image/png'} if self.purpose=='receipt' else {'application/pdf','text/plain','text/markdown'}
        if detected!=self.mime or detected not in allowed:raise FileFailure(422)

    @property
    def digest(self):return sha256(self.data).hexdigest()


def public_file(value):
    return {k:value[k] for k in ('id','purpose','mime','size_bytes','scan_state','version')}
