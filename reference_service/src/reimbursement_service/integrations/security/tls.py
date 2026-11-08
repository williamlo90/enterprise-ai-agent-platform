"""Private service CA; never disable TLS verification."""
import os,ssl
import certifi
def internal_tls():
 context=ssl.create_default_context(cafile=certifi.where())
 path=os.environ.get('EAI_INTERNAL_CA_FILE')
 if path:context.load_verify_locations(cafile=path)
 return context
