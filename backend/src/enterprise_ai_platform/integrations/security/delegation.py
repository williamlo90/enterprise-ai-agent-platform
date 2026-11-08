import base64
import json
import re
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from enterprise_ai_platform.modules.delegation.domain import validate_claims


def strict_object(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError("Duplicate signing field.")
            result[key]=value
        return result
    value=json.loads(raw.decode("utf-8") if isinstance(raw,bytes) else raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    if type(value) is not dict:raise ValueError("Invalid signing object.")
    return value


class RSADelegationVerifier:
    def header(self,token):
        if type(token) is not str or not 1<=len(token)<=16384:raise ValueError("Invalid delegation size.")
        parts=token.split(".")
        if len(parts)!=3 or any(not re.fullmatch(r"[A-Za-z0-9_-]+",p) for p in parts):raise ValueError("Invalid token.")
        header=strict_object(base64.urlsafe_b64decode(parts[0]+"="*(-len(parts[0])%4)))
        strict_object(base64.urlsafe_b64decode(parts[1]+"="*(-len(parts[1])%4)))
        if (set(header)!={"alg","typ","kid"} or header["alg"]!="RS256" or header["typ"]!="JWT"
                or type(header["kid"]) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}",header["kid"])):
            raise ValueError("Untrusted signing header.")
        return header

    def verify(self,token,jwk):
        header=self.header(token)
        if (type(jwk) is not dict or set(jwk)-{"kty","n","e","alg","use","key_ops","kid"}
                or jwk.get("kty")!="RSA" or jwk.get("alg","RS256")!="RS256"
                or jwk.get("kid",header["kid"])!=header["kid"]
                or any(type(jwk.get(k)) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,"+str(limit)+r"}",jwk[k]) for k,limit in (("n",700),("e",8)))
                or jwk.get("use","sig")!="sig" or jwk.get("key_ops",["verify"])!=["verify"]):
            raise ValueError("Invalid public verification key.")
        key=jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
        if not 2048<=key.key_size<=4096:raise ValueError("Invalid RSA size.")
        return jwt.decode(token,key,algorithms=["RS256"],options={"verify_aud":False,"verify_iss":False,
            "verify_exp":False,"verify_iat":False,"verify_nbf":False,"verify_sub":False,"verify_jti":False})


class RSADelegationSigner:
    """Backend-only signer; no anonymous issuance API or model/tool entry point."""
    def __init__(self,private_key,kid,issuer,audience,caller):
        if (not isinstance(private_key,rsa.RSAPrivateKey) or not 2048<=private_key.key_size<=4096
                or not isinstance(kid,str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}",kid)
                or any(type(value) is not str or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,"+str(limit)+r"}",value)
                       for value,limit in ((issuer,127),(audience,63),(caller,63)))):
            raise ValueError("Invalid delegation signer configuration.")
        self.private_key,self.kid,self.issuer,self.audience,self.caller=private_key,kid,issuer,audience,caller
    def __repr__(self):return "RSADelegationSigner(<redacted>)"
    def sign(self,claims,*,now):
        validate_claims(claims,issuer=self.issuer,audience=self.audience,caller=self.caller,now=now)
        token=jwt.encode(claims,self.private_key,algorithm="RS256",headers={"kid":self.kid,"typ":"JWT"})
        RSADelegationVerifier().header(token)
        return token
