"""Run only in the isolated Linux parser image; stdin is header LF file bytes."""
import io
import json
import logging
import os
import sys
import warnings
sys.path.insert(0, os.path.dirname(__file__))
from file_policy import FileRejected, inspect_bytes, read_bounded

MAX_PAGES = 100
MAX_TEXT = 1_000_000
MAX_PIXELS = 20_000_000


def parse(data, purpose, mime, extract=False):
    if extract and purpose!='knowledge':raise FileRejected('invalid_header')
    identity = inspect_bytes(data, purpose, mime)
    if mime == 'application/pdf':
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise FileRejected('encrypted_pdf')
        if not 1 <= len(reader.pages) <= MAX_PAGES:
            raise FileRejected('page_limit')
        if reader.trailer['/Root'].get('/OpenAction') or reader.trailer['/Root'].get('/AA'):
            raise FileRejected('active_pdf')
        text = '';pages=[]
        if purpose == 'knowledge':
            for number,page in enumerate(reader.pages,1):
                content=page.extract_text() or ''
                pages.append({'number':number,'text':content})
                text += content
                if len(text) > MAX_TEXT:
                    raise FileRejected('text_limit')
            if not text.strip():
                raise FileRejected('ocr_required')
        else:
            # Force page dictionaries to resolve; receipt PDF does not need OCR/text.
            for page in reader.pages:
                if not page.mediabox:
                    raise FileRejected()
        if extract and len(text.encode('utf-8'))>12288:raise FileRejected('text_limit')
        return dict(size=identity.size,sha256=identity.sha256,detected_type=mime,pages=len(reader.pages),text_characters=len(text),**({'extracted_pages':pages} if extract else {}))
    if mime.startswith('image/'):
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        warnings.simplefilter('error',Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data),formats=['PNG','JPEG']) as image:
            if image.width*image.height > MAX_PIXELS or getattr(image,'n_frames',1)!=1:
                raise FileRejected('image_limit')
            image.verify()
        with Image.open(io.BytesIO(data),formats=['PNG','JPEG']) as image:
            image.load()
        return dict(size=identity.size,sha256=identity.sha256,detected_type=mime,pages=1,text_characters=0)
    text=data.decode('utf-8',errors='strict')
    if len(text)>MAX_TEXT:
        raise FileRejected('text_limit')
    if extract and len(text.encode('utf-8'))>12288:raise FileRejected('text_limit')
    return dict(size=identity.size,sha256=identity.sha256,detected_type=mime,pages=0,text_characters=len(text),**({'extracted_pages':[{'number':1,'text':text}]} if extract else {}))


def main():
    if sys.platform != 'linux':
        print(json.dumps(dict(passed=False,code='isolated_linux_runtime_required')))
        return
    import resource
    logging.disable(logging.CRITICAL)
    resource.setrlimit(resource.RLIMIT_CPU,(5,5))
    resource.setrlimit(resource.RLIMIT_FSIZE,(8*1024*1024,8*1024*1024))
    resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))
    resource.setrlimit(resource.RLIMIT_NPROC,(32,32))
    try:
        header=sys.stdin.buffer.readline(513)
        if len(header)>512 or not header.endswith(b'\n'):
            raise FileRejected('header_limit')
        values=json.loads(header)
        if not isinstance(values,dict) or (set(values)!={'purpose','mime'} and not (set(values)=={'purpose','mime','operation'} and values['operation']=='extract')):
            raise FileRejected('invalid_header')
        result=parse(read_bounded(sys.stdin.buffer),values['purpose'],values['mime'],extract=values.get('operation')=='extract')
        print(json.dumps(dict(passed=True,**result)))
    except FileRejected as error:
        print(json.dumps(dict(passed=False,code=error.code)))
    except Exception:
        print(json.dumps(dict(passed=False,code='invalid_file')))


if __name__=='__main__':main()
