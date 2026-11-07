import json,subprocess,sys
from pathlib import Path
p=Path(__file__).parent;plan=json.loads((p/'plan.json').read_text());container='asr-en-vi-app-1';remote=str(p)
for case in plan['cases']:
 r=subprocess.run(['timeout','1050','docker','exec',container,'python',remote+'/run_case.py',case['case']])
 copy=subprocess.run(['docker','exec',container,'cat',remote+'/results/'+case['case']+'.json'],capture_output=True)
 if copy.returncode==0:(p/'results'/(case['case']+'.json')).write_bytes(copy.stdout)
 if r.returncode or copy.returncode:
  (p/'generation-failed.json').write_text(json.dumps({'case':case['case'],'process_exit':r.returncode,'copy_exit':copy.returncode}));sys.exit(1)
(p/'generation-report.md').write_text('All eight planned public-source ChatGPT cases completed; this proves generation, not semantic acceptance. Outputs in results/.\n')
s=json.loads((p/'repo-state.json').read_text())['state_ref']
subprocess.run(['python3','/home/mars/.agents/skills/pipeline/scripts/task_result.py',str(p/'generation-result.json'),'--task','notes-chatgpt-generation','--state-ref',s,'--write','pass','--report-path',str(p/'generation-report.md')],check=True)
