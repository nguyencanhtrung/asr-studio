import hashlib,json,sys,time
from pathlib import Path
from asr_studio.config import Settings
from asr_studio import notes_document,notes_pipeline,chatgpt
from asr_studio.chatgpt import ChatGPTProvider,ChatGPTError
from asr_studio.notes import assert_provider_mutation_allowed
root=Path(__file__).parent;plan=json.loads((root/'plan.json').read_text());case=next(c for c in plan['cases'] if c['case']==sys.argv[1]);source=root/'input'/case['input'];out=root/'results'/(case['case']+'.json');assert not out.exists()
assert hashlib.sha256(source.read_bytes()).hexdigest()==plan['input_sha256'][source.name]
for m in [notes_document,notes_pipeline,chatgpt]:assert hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()==plan['module_sha256'][m.__name__.split('.')[-1]+'.py']
assert notes_document.PROMPT_VERSION==plan['prompt_version']
snapshot=json.loads(source.read_text());settings=Settings.from_env();provider=ChatGPTProvider(settings.provider_dir,callback_url='',active_task_guard=lambda:assert_provider_mutation_allowed(settings.data_dir,'chatgpt'))
assert_provider_mutation_allowed(settings.data_dir,'chatgpt')
with provider.store.locked() as s:
 assert s['connection_status']=='connected';version=s['connection_version']
coverage={};started=time.monotonic();facts={'case':case['case'],'provider':plan['provider'],'model':plan['model'],'effort':plan['reasoning_effort'],'output_language':plan['output_language'],'prompt_version':plan['prompt_version'],'source_sha256':plan['input_sha256'][source.name],'source_segments':len(snapshot['segments'])}
try:
 doc=provider.generate_notes_document(snapshot=snapshot,connection_version=version,model=plan['model'],reasoning_effort=plan['reasoning_effort'],output_language=plan['output_language'],on_coverage=coverage.update)
 result=dict(facts,status='succeeded',document=doc,coverage=coverage,elapsed_seconds=round(time.monotonic()-started,3))
except ChatGPTError as e:result=dict(facts,status='failed',code=e.code,coverage=coverage,elapsed_seconds=round(time.monotonic()-started,3))
except Exception as e:result=dict(facts,status='failed',exception_type=type(e).__name__,coverage=coverage,elapsed_seconds=round(time.monotonic()-started,3))
out.write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in result.items() if k not in ['document','coverage']}),flush=True)
raise SystemExit(0 if result['status']=='succeeded' else 1)
