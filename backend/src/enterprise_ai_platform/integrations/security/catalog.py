"""Explicit v2 signer; the six-action v1 signer remains unchanged."""
import jwt
from enterprise_ai_platform.integrations.security.delegation import RSADelegationSigner,RSADelegationVerifier
from enterprise_ai_platform.modules.delegation.domain import validate_catalog_claims


class RSACatalogSigner(RSADelegationSigner):
    def sign(self,claims,*,now):
        validate_catalog_claims(claims,issuer=self.issuer,audience=self.audience,caller=self.caller,now=now)
        token=jwt.encode(claims,self.private_key,algorithm='RS256',headers={'kid':self.kid,'typ':'JWT'})
        RSADelegationVerifier().header(token)
        return token
