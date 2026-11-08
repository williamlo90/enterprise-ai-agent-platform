from uuid import UUID

class OrganizationFailure(RuntimeError):
    def __init__(self,status=503):self.status=status;super().__init__('Organization operation rejected.')

def identifier(value):
    try:
        if not isinstance(value,str) or str(UUID(value))!=value:raise ValueError()
    except (ValueError,TypeError,AttributeError):raise OrganizationFailure(400) from None
    return value

def label(value,maximum=100,byte_max=400):
    if value is None:return None
    try:
        if not isinstance(value,str):raise ValueError()
        value=value.strip()
        if not 1<=len(value)<=maximum or len(value.encode('utf-8'))>byte_max or any(ord(c)<32 or ord(c)==127 for c in value):raise ValueError()
    except (ValueError,UnicodeError):raise OrganizationFailure(400) from None
    return value
