import { useCallback,useState } from 'react';
import { Button } from '../components/Button';
import { SelectField,TextField } from '../components/Fields';
import { StatusMessage } from '../components/StatusMessage';
import { usePreferences } from '../app/preference-context';
import { decodeSearch,searchPath } from './source-api';
import { SourceWorkspace,PrivateReadError } from './SourceWorkspace';
import { useCopy } from './workspace-hooks';
import { usePrivateRead } from './use-private-read';

export function SourceSearch() {
  const c=useCopy();const {locale}=usePreferences();const [query,setQuery]=useState('');const [language,setLanguage]=useState<'id'|'en'>(locale);const [path,setPath]=useState<string|null>(null);const [request,setRequest]=useState(0);const [invalid,setInvalid]=useState(false);
  const result=usePrivateRead(path,decodeSearch,request);
  const clear=useCallback(() => {setPath(null);setQuery('');setRequest((value) => value+1);},[]);
  function submit() {try {const next=searchPath(query,language);setInvalid(false);setPath(next);setRequest((value) => value+1);} catch {setInvalid(true);setPath(null);}}
  return <SourceWorkspace onUnavailable={clear}>{(open,clearSource) => <><div className="page-heading"><h1>{c('Cari sumber','Find sources')}</h1><p>{c('Cari kata dalam dokumen yang dapat Anda baca. Buka hasil untuk memeriksa teks sumber.','Find words in documents you can read. Open a result to inspect its source text.')}</p></div>
    <form className="workspace-filters" onSubmit={(event) => {event.preventDefault();if (!result.cooling) {clearSource();submit();}}}>
      <TextField label={c('Kata pencarian','Search words')} value={query} onChange={(event) => setQuery(event.target.value)} error={invalid?c('Gunakan 1–200 karakter, maksimal 800 byte, tanpa karakter kontrol.','Use 1–200 characters, at most 800 bytes, without control characters.'):undefined} autoComplete="off" />
      <SelectField label={c('Bahasa pencarian','Search language')} value={language} onChange={(event) => setLanguage(event.target.value as 'id'|'en')} options={[{value:'id',label:'Bahasa Indonesia'},{value:'en',label:'English'}]} />
      <Button type="submit" disabled={result.cooling}>{c('Cari','Search')}</Button>
    </form><p className="field-hint">{c('Pencarian kata sederhana, tanpa terjemahan otomatis.','Simple word search, without automatic translation.')}</p>
    {result.loading && <StatusMessage kind="loading" title={c('Mencari sumber','Searching sources')} />}
    {result.error && <PrivateReadError error={result.error} cooling={result.cooling} retry={() => setRequest((value) => value+1)} />}
    {result.data && (result.data.items.length===0?<StatusMessage kind="empty" title={c('Tidak ada sumber yang cocok','No matching sources')}><p>{c('Coba kata lain. Hanya sumber yang dapat Anda baca yang ditampilkan.','Try different words. Only sources you can read are shown.')}</p></StatusMessage>:
      <ul className="source-results">{result.data.items.map((hit) => <li key={hit.version_id+':'+hit.chunk_no}><h2><Button variant="quiet" onClick={(event) => open(hit,event.currentTarget)}>{hit.title}</Button></h2><p className="field-hint">{c('Versi ','Version ')+hit.version_no} · {c('Halaman ','Page ')+hit.page} · {c('Chunk ','Chunk ')+hit.chunk_no}</p><p className="source-excerpt">{hit.text}</p></li>)}</ul>)}
  </>}</SourceWorkspace>;
}
