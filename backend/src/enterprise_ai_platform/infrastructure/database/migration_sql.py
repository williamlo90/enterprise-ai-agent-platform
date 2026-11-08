"""Preflight for authored transactional migrations, not a sandbox for untrusted SQL."""

import re


def statement_words(source):
    words=[];index=0;length=len(source)
    while index<length:
        char=source[index]
        if char.isspace():index+=1;continue
        if source.startswith("--",index):
            end=re.search(r"[\r\n]",source[index+2:]);index=length if end is None else index+2+end.end();continue
        if source.startswith("/*",index):
            depth=1;index+=2
            while index<length and depth:
                if source.startswith("/*",index):depth+=1;index+=2
                elif source.startswith("*/",index):depth-=1;index+=2
                else:index+=1
            if depth:raise ValueError("Unterminated migration comment.")
            continue
        if char in {"'",'"'}:
            quote=char
            escaped=quote=="'" and index>0 and source[index-1] in "eE" and (index==1 or not re.match(r"[A-Za-z0-9_$]",source[index-2]))
            index+=1
            while index<length:
                if escaped and source[index]=="\\":index+=2;continue
                if source[index]==quote:
                    if index+1<length and source[index+1]==quote:index+=2;continue
                    index+=1;break
                index+=1
            else:raise ValueError("Unterminated migration string.")
            words.append("QUOTED")
            continue
        if char=="$":
            tag=re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$",source[index:])
            if tag:
                marker=tag[0];end=source.find(marker,index+len(marker))
                if end<0:raise ValueError("Unterminated migration body.")
                index=end+len(marker);words.append("QUOTED");continue
        if char==";":
            if words:yield words
            words=[];index+=1;continue
        word=re.match(r"[A-Za-z_][A-Za-z0-9_$]*",source[index:])
        if word:
            words.append(word[0].upper());index+=len(word[0]);continue
        if char=="\\":raise ValueError("Client commands are not migration SQL.")
        index+=1
    if words:yield words


def validate_atomic_sql(source):
    statements=list(statement_words(source))
    if not statements:raise ValueError("Migration must contain SQL.")
    for words in statements:
        first=words[0]
        if (first in {"BEGIN","COMMIT","END","ABORT","ROLLBACK","SAVEPOINT","RELEASE","VACUUM","COPY","RESET","DISCARD"}
                or words[:2] in (["START","TRANSACTION"],["PREPARE","TRANSACTION"],["ALTER","SYSTEM"])
                or (first in {"CREATE","DROP"} and len(words)>1 and words[1] in {"DATABASE","TABLESPACE"})
                or (first in {"CREATE","DROP","REINDEX"} and "CONCURRENTLY" in words[:6])
                or (first=="SET" and (len(words)<2 or words[1] not in {"LOCAL","CONSTRAINTS"} or "STANDARD_CONFORMING_STRINGS" in words[:4]))):
            raise ValueError("Migration requires a separate operational procedure.")
