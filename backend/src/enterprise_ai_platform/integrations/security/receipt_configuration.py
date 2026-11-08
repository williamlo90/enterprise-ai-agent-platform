"""Parse operator-injected trust/key material before allocating resources."""
import json
import re
from enterprise_ai_platform.integrations.security.receipt_terminal import ReceiptTerminalVerifier,TerminalKey
from enterprise_ai_platform.integrations.security.receipt_settlement import SettlementKey


def receipt_security(raw):
    try:
        if type(raw) is not str or len(raw.encode())>16384:raise ValueError
        def unique(pairs):
            result={}
            for k,v in pairs:
                if k in result:raise ValueError
                result[k]=v
            return result
        value=json.loads(raw,object_pairs_hook=unique)
        if type(value) is not dict or set(value)!={'credential_hash','key_id','key_hex','not_before','not_after','terminal_keys'}:raise ValueError
        if type(value['credential_hash']) is not str or not re.fullmatch('[a-f0-9]{64}',value['credential_hash']):raise ValueError
        if type(value['key_hex']) is not str or not re.fullmatch('[a-f0-9]{64}',value['key_hex']):raise ValueError
        key=SettlementKey(value['key_id'],bytes.fromhex(value['key_hex']),value['not_before'],value['not_after'])
        registry=value['terminal_keys']
        if type(registry) is not dict or not 1<=len(registry)<=8:raise ValueError
        keys={}
        for kid,row in registry.items():
            if type(row) is not dict or set(row)!={'public_hex','not_before','not_after','revoked'}:raise ValueError
            if type(row['public_hex']) is not str or not re.fullmatch('[a-f0-9]{64}',row['public_hex']):raise ValueError
            keys[kid]=TerminalKey(bytes.fromhex(row['public_hex']),row['not_before'],row['not_after'],row['revoked'])
        return key,value['credential_hash'],ReceiptTerminalVerifier(keys)
    except Exception:raise ValueError('Invalid private receipt security configuration.') from None
