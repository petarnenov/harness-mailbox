'use strict';
const $ = id => document.getElementById(id);
let data;
let request = 0;
let focusId = '';
let focusSubtree = false;
let comparison = null;
let detailReturnFocus = null;
let urlStateErrors = [];
const processStages={uninvestigated:'Uninvestigated',investigating:'Investigating',ready:'Ready for gated execution',implementation:'Implementation',review:'Review',verification:'Verification',complete:'Verified complete',deferred:'Deferred',unknown:'Stage unknown'};
const labels = {pending:'Pending',in_progress:'In progress',blocked:'Blocked',awaiting_verification:'Awaiting verification',verified_done:'Verified done',deferred:'Deferred',done:'Done'};
function el(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
function moneyless(n) { return n===null || n===undefined ? 'Unavailable' : Number(n).toLocaleString(undefined,{maximumFractionDigits:2}); }
function range(r) { return r ? `${moneyless(r.low)} / ${moneyless(r.likely)} / ${moneyless(r.high)} h` : 'Unavailable'; }
function status(s) { return el('span',labels[s]||s,'status '+(Object.hasOwn(labels,s)?s:'')); }
function empty(target,title,detail) { const box=el('div',undefined,'empty');box.append(el('strong',title),el('span',detail));target.append(box); }
function issueEstimate(i) { return i.estimate || Object.fromEntries(['low','likely','high'].map(k=>[k,data.estimate_method[k]*data.estimate_method.review_multiplier])); }
function sumMetrics(items,complete) {
 const verified=items.filter(i=>i.verified).length;
 const estimate={low:0,likely:0,high:0},remaining={...estimate};let measured=0,actual=0;
 items.forEach(i=>{ const e=issueEstimate(i);Object.keys(e).forEach(k=>{estimate[k]+=e[k];if(!i.verified)remaining[k]+=e[k];});if(i.actual_machine_hours!==null){measured++;actual+=i.actual_machine_hours;} });
 return {known_issues:items.length,verified_issues:verified,percent:complete&&items.length?Math.round(verified/items.length*1000)/10:null,estimate:items.length?estimate:null,remaining_estimate:items.length?remaining:null,actual_machine_hours:measured?actual:null,measured_issues:measured,scope_complete:complete};
}
const openTaskStatuses=['pending','in_progress','blocked'];
function isOpenFreeTextTask(record) {return record.mapping_kind==='unmapped'&&openTaskStatuses.includes(record.status);}
function freeTextProcessGroup(task) {return task.status==='blocked'||(task.blockers||[]).length?'blocked':task.status==='in_progress'?'now':'next';}
const settledCompletionMinutes=5;
function hasOpenDependents(nodeId) {const ready=new Set((data.graph?.nodes||[]).filter(n=>n.ready===true).map(n=>n.id));return (data.graph?.edges||[]).some(e=>e.kind==='requires'&&e.from===nodeId&&!ready.has(e.to));}
function completedLongerThan(record,minutes,now=Date.now()) {const at=Date.parse(record.verified_at||record.updated_at||'');return !Number.isFinite(at)||now-at>minutes*60000;}
function isSettledCompletedTask(task) {return task.evidenced===true&&!hasOpenDependents('task:'+task.id)&&completedLongerThan(task,settledCompletionMinutes);}
function isSettledCompletedIssue(issue) {return issue.verified===true&&!hasOpenDependents('issue:'+issue.key)&&completedLongerThan(issue,settledCompletionMinutes);}
function settledCompletedNodeIds() {return new Set([...data.issues.filter(isSettledCompletedIssue).map(i=>'issue:'+i.key),...data.tasks.filter(isSettledCompletedTask).map(t=>'task:'+t.id)]);}
function matchesTeam(record,team) { const teams=record.teams||[];return !team||(team==='unassigned'?teams.length===0:teams.includes(team)); }
function selection() {
 const p=$('project').value,e=$('epic').value,team=$('team').value,q=$('search').value.trim().toLowerCase(),hideCompleted=$('hide-completed').checked;
 const taskId=e.startsWith('task:')?e.slice(5):'',epic=taskId?'':e;
 const invalid=[...urlStateErrors];if(p&&!data.projects.some(x=>x.id===p))invalid.push('Project: '+p);if(epic&&!data.epics.some(x=>x.key===epic))invalid.push('Epic: '+epic);if(taskId&&!data.tasks.some(t=>t.id===taskId))invalid.push('Task: '+taskId);if(!['','BE','FE','QA','unassigned'].includes(team))invalid.push('Team: '+team);
 const found=i=>!q||(i.key+' '+i.title).toLowerCase().includes(q);
 const scopedIssues=invalid.length?[]:data.issues.filter(i=>(!p||i.projects.includes(p))&&(!epic||i.epic_keys.includes(epic))&&(!taskId||i.task_ids.includes(taskId))&&matchesTeam(i,team)&&found(i));
 const issues=scopedIssues.filter(i=>!(hideCompleted&&isSettledCompletedIssue(i)));
 const inTaskScope=(t,linkedIssues)=>(!p||t.project===p)&&(!epic||(t.epic_keys||[t.epic_key]).includes(epic))&&(!taskId||t.id===taskId)&&matchesTeam(t,team)&&(!q||(t.id+' '+t.title+' '+(t.issue_keys||[]).join(' ')+' '+(t.epic_keys||[]).join(' ')).toLowerCase().includes(q)||linkedIssues.some(i=>i.task_ids.includes(t.id)));
 const scopedTasks=invalid.length?[]:data.tasks.filter(t=>inTaskScope(t,scopedIssues));
 const tasks=invalid.length?[]:data.tasks.filter(t=>inTaskScope(t,issues)&&!(hideCompleted&&isSettledCompletedTask(t)));
 const reported=invalid.length||q||taskId?[]:data.reported_statistics.filter(r=>(!p||!r.project||r.project===p)&&(!epic||!r.epic||r.epic===epic)&&(!team||((r.teams||[]).length>0&&matchesTeam(r,team))));
 const epics=invalid.length?[]:data.epics.filter(x=>(!epic||x.key===epic)&&(!(p||team||q||taskId)||issues.some(i=>i.epic_keys.includes(x.key))||tasks.some(t=>(t.epic_keys||[t.epic_key]).includes(x.key))||reported.some(r=>r.epic===x.key&&(!p||r.project===p))));
 const projects=invalid.length?[]:data.projects.filter(x=>(!p||x.id===p)&&(!team||matchesTeam(x,team)||scopedIssues.some(i=>i.projects.includes(x.id))||scopedTasks.some(t=>t.project===x.id)||reported.some(r=>r.project===x.id))&&(!(e||q)||scopedIssues.some(i=>i.projects.includes(x.id))||scopedTasks.some(t=>t.project===x.id)||reported.some(r=>r.project===x.id&&r.epic===epic)));
 const complete=!invalid.length&&!taskId&&(epic?data.epics.some(x=>x.key===epic)&&data.epics.filter(x=>x.key===epic).every(x=>x.scope_complete):data.totals.scope_complete);
 return {p,e:epic,taskId,team,q,hideCompleted,invalid,issues,tasks,scopedIssues,scopedTasks,epics,projects,reported,totals:sumMetrics(scopedIssues,complete)};
}
function taskShortLabel(task) {const words=String(task.short_title||task.title||task.id).trim().split(/\s+/);return words.length>4?words.slice(0,4).join(' ')+'…':words.join(' ');}
function populateWorkFilter() {
 const select=$('epic'),previous=select.value;select.replaceChildren();const all=el('option','All tasks');all.value='';select.append(all);
 const groups=[['Epics',data.epics.map(x=>[x.key,x.key])],['Free-text tasks',data.tasks.filter(t=>t.mapping_kind==='unmapped').map(t=>['task:'+t.id,taskShortLabel(t)])]];
 groups.forEach(([label,items])=>{if(!items.length)return;const group=document.createElement('optgroup');group.label=label;items.forEach(([value,text])=>{const o=el('option',text);o.value=value;group.append(o);});select.append(group);});
 selectValue('epic',previous);
}
function getDelivery(issue) {
 const raw=issue?.delivery&&typeof issue.delivery==='object'?issue.delivery:{};
 const verification=raw.verification&&typeof raw.verification==='object'?raw.verification:{};
 const freshness=raw.freshness&&typeof raw.freshness==='object'?raw.freshness:{};
 return {stage:Object.hasOwn(processStages,raw.stage)?raw.stage:'unknown',stage_reason:raw.stage_reason||'No process stage is recorded.',blocked:raw.blocked===true,blockers:Array.isArray(raw.blockers)?raw.blockers:[],existing_work:raw.existing_work||'Existing work has not been recorded.',remainder:raw.remainder||'Remaining work is unknown.',owner:raw.owner||null,next_action:raw.next_action||'No next action is recorded; consult the owning project.',acceptance_criteria:raw.acceptance_criteria||'',criteria:Array.isArray(raw.criteria)?raw.criteria:[],verification:{status:verification.status||'unknown',verifier:verification.verifier||null,evidence:Array.isArray(verification.evidence)?verification.evidence:[],reason:verification.reason||'Verification evidence has not been recorded.'},trace:Array.isArray(raw.trace)?raw.trace:[],freshness,source:raw.source||issue?.sources?.join(', ')||'Source unavailable'};
}
function stageMarker(issue) {
 const delivery=getDelivery(issue),stage=delivery.stage;
 const kind=stage==='complete'&&issue.verified?'complete':['implementation','investigating'].includes(stage)?'progress':['review','verification','ready'].includes(stage)?'waiting':stage==='deferred'?'deferred':'unknown';
 return {label:stage==='complete'&&!issue.verified?'Stage unknown':processStages[stage],kind};
}
function issueButton(issue,label) {const button=el('button',label||issue.key+' · '+issue.title,'issue-button');button.type='button';button.dataset.issueKey=issue.key;button.addEventListener('click',()=>openIssue(issue.key));return button;}
function ownershipRoles(issue,full=false) {
 const ownership=issue.delivery?.ownership,area=el('div',undefined,'ownership-roles');
 const definitions=[['jira','Jira assignee','not_loaded'],['implementation','Implementation owner/project','not_recorded'],['execution','Recorded execution owner','not_recorded']];
 definitions.forEach(([key,label,missing])=>{
  const raw=ownership?.[key],role=raw&&typeof raw==='object'&&!Array.isArray(raw)?raw:{},members=Array.isArray(role.members)?role.members.filter(member=>member&&typeof member==='object'&&!Array.isArray(member)):[];
  const recordedName=typeof role.name==='string'?role.name.trim():'',memberNames=members.map(member=>typeof member.name==='string'?member.name.trim():'').filter(Boolean);
  const compactNames=key==='execution'?members.map(member=>{const memberName=typeof member.name==='string'?member.name.trim():'',identity=memberName||member.identity;if(typeof identity!=='string')return '';if(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(identity))return (typeof member.project==='string'&&member.project?member.project+' · ':'')+'recorded session '+identity.slice(0,8);return memberName||identity;}).filter(Boolean):[];
  const name=compactNames.length?[...new Set(compactNames)].join('; '):recordedName||[...new Set(memberNames)].join('; ');
  const allowed=key==='jira'?['assigned','unassigned','not_loaded','conflict']:['assigned','not_recorded','conflict'];
  let state=raw===undefined?missing:allowed.includes(role.status)?role.status:'conflict';if(state==='assigned'&&!name)state='conflict';
  const text=state==='assigned'?name:state==='unassigned'?'Unassigned in Jira':state==='not_loaded'?'Not loaded':state==='not_recorded'?'Not recorded':'Conflicting records';
  const row=el('div',undefined,'ownership-role');row.dataset.role=key;row.dataset.status=state;
  if(full){const value=el('p',undefined,'ownership-value');value.append(el('span',label+': '),el('strong',text));row.append(value);}else row.append(el('p',label+': '+text));
  if(full){
   const audit=el('details',undefined,'ownership-audit');audit.append(el('summary','Sources & recorded identities'));row.append(audit);
   const explanation=typeof role.reason==='string'&&role.reason?role.reason:state==='not_loaded'?'Jira assignee metadata has not been loaded.':state==='not_recorded'?'No ownership record is available for this role.':state==='conflict'?'Ownership records are conflicting or malformed; no owner is selected.':'Recorded ownership snapshot.';
   audit.append(el('p',explanation),el('p','Ownership source: '+(typeof role.source==='string'&&role.source?role.source:'Not recorded')));
   if(key==='jira'){audit.append(el('p','Ownership snapshot retrieved: '+timestampDescription(role.retrieved_at)));if(Array.isArray(role.observations))role.observations.forEach(observation=>{if(observation&&typeof observation==='object')audit.append(el('p','Ownership observation source: '+(typeof observation.source==='string'?observation.source:'Not recorded')+' · retrieved: '+timestampDescription(observation.retrieved_at)));});}
   members.forEach(member=>{const record=el('article',undefined,'criterion');record.append(el('p','Recorded name: '+(typeof member.name==='string'?member.name:'Not recorded')),el('p','Recorded identity: '+(typeof member.identity==='string'?member.identity:'Not recorded')),el('p','Ownership source: '+(typeof member.source==='string'&&member.source?member.source:'Not recorded')));[['task_id','Linked task'],['project','Project context'],['task_status','Task snapshot status']].forEach(([field,title])=>{if(typeof member[field]==='string'&&member[field])record.append(el('p',title+': '+member[field]));});audit.append(record);});
   if(key==='execution')audit.append(el('p','Execution identities are recorded worker snapshots; they do not establish current liveness or reachability.'));
  }
  area.append(row);
 });
 const legacyOwner=typeof issue.delivery?.owner==='string'?issue.delivery.owner.trim():'';
 const represented=legacyOwner&&['implementation','execution'].some(key=>{const role=ownership?.[key];return role?.status==='assigned'&&(role.name===legacyOwner||(Array.isArray(role.members)&&role.members.some(member=>member&&(member.name===legacyOwner||member.identity===legacyOwner))));});
 if(legacyOwner&&!represented)area.append(el('p','Recorded local owner (legacy): '+legacyOwner));
 return area;
}
function compactIssueCards(area,issues,makeCard) {
 const order={implementation:0,review:1,verification:2,investigating:3,ready:4,uninvestigated:5,unknown:6,deferred:7,complete:8};
 const sorted=[...issues].sort((a,b)=>(order[getDelivery(a).stage]-order[getDelivery(b).stage])||a.key.localeCompare(b.key));
 area.append(el('p',`${sorted.length} recorded issues`,'muted'));const remaining=el('details',undefined,'remaining-work');
 if(sorted.length>5)remaining.append(el('summary',`Show remaining ${sorted.length-5}`));
 sorted.forEach((issue,index)=>{const card=makeCard(issue);(index<5?area:remaining).append(card);});if(sorted.length>5)area.append(remaining);
}
function renderProcess(s) {
 const strip=$('process-strip');strip.replaceChildren();Object.entries(processStages).forEach(([stage,label])=>{const issueCount=s.issues.filter(i=>getDelivery(i).stage===stage).length,taskCount=s.tasks.filter(t=>t.mapping_kind==='unmapped'&&t.delivery_stage===stage).length,box=el('div',undefined,'process-stage');box.dataset.stage=stage;box.append(el('strong',String(issueCount+taskCount)),el('span',label));if(taskCount)box.append(el('small',`${issueCount} issues · ${taskCount} free-text tasks`));strip.append(box);});
 const groups={now:[],blocked:[],next:[]};s.issues.forEach(issue=>{const d=getDelivery(issue);if(d.blocked)groups.blocked.push(issue);else if(['investigating','implementation','review','verification'].includes(d.stage))groups.now.push(issue);else if(!['complete','deferred'].includes(d.stage))groups.next.push(issue);});
 Object.entries(groups).forEach(([group,issues])=>{const area=$('process-'+group);area.replaceChildren();compactIssueCards(area,issues,issue=>{const d=getDelivery(issue),marker=stageMarker(issue),card=el('article',undefined,'process-card');card.append(issueButton(issue),issueTimeLine(issue),el('span',marker.label,'graph-status marker-'+marker.kind),ownershipRoles(issue),el('p',d.next_action));if(d.blocked)card.append(el('span','! Blocked','graph-status marker-blocked'),el('p','Blockers: '+(d.blockers.join('; ')||'Blocked; reason not recorded'),'process-blocker'));card.append(el('small','Recorded suggestion · dependency gate required'));return card;});const freeText=s.tasks.filter(t=>isOpenFreeTextTask(t)&&freeTextProcessGroup(t)===group);appendFreeTextTasks(area,freeText);if(!issues.length&&!freeText.length)area.append(el('p','No matching recorded work.','muted'));});
 renderFreeTextOverview(s);
 const queue=$('verification-queue');queue.replaceChildren();const pending=s.issues.filter(i=>getDelivery(i).stage==='verification'||i.local_status==='awaiting_verification');compactIssueCards(queue,pending,issue=>{const d=getDelivery(issue),card=el('article',undefined,'process-card');card.append(issueButton(issue),issueTimeLine(issue),ownershipRoles(issue),el('p',criteriaSummary(d)),el('p','Verifier: '+(d.verification.verifier||'Unknown')),el('p',d.verification.reason));return card;});if(!pending.length)queue.append(el('p','No matching issues await recorded verification.','muted'));
}
function appendFreeTextTasks(area,tasks) {
 if(!tasks.length)return;area.append(el('p',`${tasks.length} free-text tasks`,'muted'));
 tasks.forEach(t=>{const card=el('article',undefined,'process-card');card.dataset.status=t.status;card.append(el('strong',taskDisplayTitle(t)),status(t.status),el('p',`${t.project} · Free-text task · ${t.id.slice(0,8)}`));if((t.blockers||[]).length)card.append(el('p','Blockers: '+t.blockers.join('; '),'process-blocker'));area.append(card);});
}
function renderFreeTextOverview(s) {
 const area=$('free-text-tasks'),tasks=s.tasks.filter(t=>t.mapping_kind==='unmapped');area.replaceChildren();
 $('free-text-count').textContent=tasks.length?`${tasks.length} tasks · ${executionSummary(tasks)}`:'';
 tasks.forEach(t=>area.append(taskCard(t)));
 if(!tasks.length)empty(area,'No free-text tasks','Tasks created from free text without a Jira key appear here.');
}
function criteriaSummary(delivery) {return delivery.criteria.length?`${delivery.criteria.filter(c=>Array.isArray(c.evidence)&&c.evidence.length).length} / ${delivery.criteria.length} criteria have recorded evidence; individual outcomes remain explicit.`:'Criteria coverage unknown — no structured criterion evidence recorded.';}
function sourceCriteria(text) {
 const source=String(text||'').replace(/\r\n?/g,'\n');
 const starts=[...source.matchAll(/^\s*\d+[:.)]\s+[^\n]+/gm)];
 if(!starts.length)return {context:'',items:source.split(/\n\s*\n|\n(?=\s*[-*•]\s)/).filter(t=>t.trim()).map(text=>({text:text.trim(),status:'not assessed',evidence:[]}))};
 return {context:source.slice(0,starts[0].index).trim(),items:starts.map((match,index)=>({text:source.slice(match.index,starts[index+1]?.index??source.length).trim(),status:'not assessed',evidence:[]}))};
}
function criterionContent(target,text) {
 const lines=String(text||'Criterion text unavailable').split('\n');
 const numbered=lines[0].match(/^\s*\d+[:.)]\s+(.+)$/);
 if(numbered){target.append(el('h4',numbered[1],'acceptance-title'));lines.shift();}
 const steps=el('div',undefined,'acceptance-steps');
 lines.forEach(line=>{
  if(!line.trim())return;
  const match=line.match(/^\s*(Given|When|Then|And|But)\b\s*(.*)$/i);
  if(match){const step=el('div',undefined,'acceptance-step');step.dataset.keyword=match[1].toLowerCase();step.append(el('span',match[1],'acceptance-keyword'),el('p',match[2]));steps.append(step);}
  else steps.append(el('p',line,'acceptance-note'));
 });
 target.append(steps);
}
function timestampDescription(value,now=Date.now()) {
 if(value===null||value===undefined||value==='')return 'Timestamp unavailable';
 const match=typeof value==='string'&&value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|([+-])(\d{2}):?(\d{2}))$/);
 if(!match)return 'Invalid timestamp: '+String(value);
 const [,year,month,day,hour,minute,second,fraction,zone,sign,offsetHour,offsetMinute]=match;
 const leap=Number(year)%4===0&&(Number(year)%100!==0||Number(year)%400===0),days=[31,leap?29:28,31,30,31,30,31,31,30,31,30,31];
 if(Number(month)<1||Number(month)>12||Number(day)<1||Number(day)>days[Number(month)-1]||Number(hour)>23||Number(minute)>59||Number(second)>59||(zone!=='Z'&&(Number(offsetHour)>23||Number(offsetMinute)>59)))return 'Invalid timestamp: '+String(value);
 const normalized=year+'-'+month+'-'+day+'T'+hour+':'+minute+':'+second+(fraction?'.'+fraction.slice(0,3).padEnd(3,'0'):'')+(zone==='Z'?'Z':sign+offsetHour+':'+offsetMinute);
 const instant=Date.parse(normalized);if(!Number.isFinite(instant))return 'Invalid timestamp: '+String(value);
 if(instant>now)return 'Future timestamp: '+String(value)+' — verify source clock';
 const minutes=Math.floor((now-instant)/60000),age=minutes<1?'less than a minute':minutes<60?minutes+' min':minutes<1440?Math.floor(minutes/60)+' h':Math.floor(minutes/1440)+' d';return String(value)+' · '+age+' old';
}
function addEvidence(target,items) {(items||[]).forEach(item=>target.append(el('p','Evidence: '+String(item))));}
function addDetailSection(body,title,text) {const section=el('section',undefined,'detail-section');section.append(el('h3',title));if(text)section.append(el('p',String(text)));body.append(section);return section;}
function renderIssueDetail(key) {
 const body=$('issue-detail-body'),issue=data.issues.find(i=>i.key===key);body.replaceChildren();if(!issue){$('issue-detail-title').textContent=key;body.append(el('p','This issue is unavailable in the current snapshot.'));return;}
 const d=getDelivery(issue),marker=stageMarker(issue);$('issue-detail-title').textContent=issue.key+' · '+issue.title;
 const intro=addDetailSection(body,'Recorded process');intro.classList.add('detail-hero');intro.prepend(el('span','DELIVERY SNAPSHOT','detail-eyebrow'));
 const badges=el('div',undefined,'detail-badges');badges.append(el('span',marker.label,'graph-status marker-'+marker.kind),el('span','Jira snapshot status: '+issue.source_status,'detail-badge detail-jira'),el('span','Issue verification: '+(issue.verified?'Verified complete':'Not verified'),'detail-badge '+(issue.verified?'marker-complete':'detail-unverified')));intro.append(badges,issueTimeLine(issue),el('p',d.stage_reason));if(d.blocked)intro.append(el('span','! Blocked','graph-status marker-blocked'),el('p','Blocked: '+(d.blockers.join('; ')||'Reason not recorded'),'process-blocker'));
 const next=addDetailSection(body,'Next action',d.next_action);next.classList.add('detail-next-action');next.prepend(el('span','YOUR NEXT STEP','detail-eyebrow'));next.append(el('small','Recorded suggestion · execution requires the dependency gate and project loop.'));
 const owners=addDetailSection(body,'Ownership');owners.classList.add('detail-ownership');owners.append(ownershipRoles(issue,true));
 const workGrid=el('div',undefined,'detail-work-grid');body.append(workGrid);const existing=addDetailSection(workGrid,'Existing work',d.existing_work),remaining=addDetailSection(workGrid,'Remaining work',d.remainder);existing.classList.add('detail-existing');remaining.classList.add('detail-remaining');
 const dependencies=addDetailSection(body,'Prerequisites & dependents');const id='issue:'+issue.key,links=(data.graph?.edges||[]).filter(e=>e.kind==='requires'&&(e.from===id||e.to===id));
 links.forEach(edge=>{const other=(data.graph.nodes||[]).find(n=>n.id===(edge.to===id?edge.from:edge.to)),line=edge.to===id?'Prerequisite: ':'Dependent: ';dependencies.append(el('p',line+(other?graphRecordLabel(other):edge.to===id?edge.from:edge.to)+' · '+(edge.condition||'Recorded dependency')));addEvidence(dependencies,edge.evidence);});if(!links.length)dependencies.append(el('p','No direct prerequisite or dependent is recorded; this does not establish execution eligibility.'));
 const focus=el('button','Focus this issue in dependency map');focus.type='button';focus.id='focus-issue';focus.addEventListener('click',()=>{focusId=id;focusSubtree=false;closeIssue();$('graph-execution').setAttribute('aria-pressed','true');$('graph-hierarchy').setAttribute('aria-pressed','false');writeUrl('dependencies');selectView();render();});dependencies.append(focus);
 const work=addDetailSection(body,'Linked local work');const tasks=data.tasks.filter(t=>(issue.task_ids||[]).includes(t.id));tasks.forEach(t=>{const row=el('article',undefined,'criterion');row.append(el('strong',`Local task · ${t.id}`),el('p',taskDisplayTitle(t)+' · '+(labels[t.status]||t.status)+' · '+(t.evidenced?'completion evidence recorded':'completion evidence unconfirmed')),el('p',taskAssociation(t)),el('p','Source: '+(t.source||'Unknown')));const evidence=Array.isArray(t.evidence)?t.evidence:(data.graph?.nodes||[]).find(n=>n.id==='task:'+t.id)?.evidence||[];addEvidence(row,evidence);if(!evidence.length)row.append(el('p','Evidence references unavailable.'));work.append(row);});if(!tasks.length)work.append(el('p','No linked local tasks recorded.'));
 const acceptance=addDetailSection(body,'Acceptance criteria');
 acceptance.append(el('span','DEFINITION OF DONE','detail-eyebrow'));
 const structured=d.criteria.length>0;
 const source=sourceCriteria(d.acceptance_criteria);
 const criteria=structured?d.criteria:source.items;
 const covered=d.criteria.filter(c=>Array.isArray(c.evidence)&&c.evidence.length).length;
 const overview=el('div',undefined,'acceptance-overview');
 overview.append(el('strong',structured?`${covered} / ${criteria.length}`:String(criteria.length)),el('span',structured?'criteria with recorded evidence':'source scenarios'));
 acceptance.append(overview);
 if(structured)acceptance.append(el('p',criteriaSummary(d),'acceptance-caption'));
 else acceptance.append(el('p','Scenario outcomes have not been assessed.','acceptance-caption'));
 if(!structured&&source.context)acceptance.append(el('p',source.context,'acceptance-context'));
 const list=el('div',undefined,'acceptance-list');acceptance.append(list);
 criteria.forEach((criterion,index)=>{
  const evidence=Array.isArray(criterion.evidence)?criterion.evidence:[],status=String(criterion.status||'unknown');
  const tone=['passed','pass','verified'].includes(status.toLowerCase())&&evidence.length?'passed':['failed','fail','blocked'].includes(status.toLowerCase())?'failed':'unknown';
  const row=el('article',undefined,'acceptance-card');row.dataset.outcome=tone;
  const heading=el('div',undefined,'acceptance-card-heading');
  heading.append(el('span',String(index+1).padStart(2,'0'),'acceptance-number'),el('span',(tone==='passed'?'✓ ':tone==='failed'?'! ':'○ ')+(structured?'Recorded: '+status:'Not assessed'),'acceptance-status'));
  row.append(heading);criterionContent(row,criterion.text);
  if(evidence.length){const audit=el('details',undefined,'acceptance-evidence');audit.append(el('summary',`Evidence · ${evidence.length} reference${evidence.length===1?'':'s'}`));addEvidence(audit,evidence);row.append(audit);}
  else if(structured)row.append(el('span','No criterion evidence recorded','acceptance-missing'));
  list.append(row);
 });
 if(!criteria.length)acceptance.append(el('p','Original acceptance criteria unavailable.','acceptance-empty'));
 if(structured&&d.acceptance_criteria){const original=el('details',undefined,'acceptance-original');original.append(el('summary','Original acceptance criteria'),el('p',String(d.acceptance_criteria)));acceptance.append(original);}

 const verification=addDetailSection(body,'Verification',d.verification.reason);verification.append(el('p','Recorded status: '+d.verification.status),el('p','Verifier: '+(d.verification.verifier||'Unknown')));addEvidence(verification,d.verification.evidence);
 const trace=addDetailSection(body,'Revision traceability');[['written','Written'],['reviewed','Reviewed'],['integrated','Integrated'],['tested_integrated','Tested integrated revision']].forEach(([step,label])=>{const record=d.trace.find(t=>t.step===step),row=el('article',undefined,'trace-step');row.dataset.traceStep=step;row.append(el('strong',label),el('p','Status: '+(record?.status||'unknown')),el('p','Revision: '+(record?.revision||'Unknown')),el('p','Evidence time: '+timestampDescription(record?.at)),el('p',record?.reason||'No traceability reason recorded.'));addEvidence(row,Array.isArray(record?.evidence)?record.evidence:[]);trace.append(row);});
 trace.classList.add('detail-trace');acceptance.classList.add('detail-acceptance');verification.classList.add('detail-verification');dependencies.classList.add('detail-dependencies');work.classList.add('detail-local-work');
 const freshness=el('details',undefined,'detail-source-audit');freshness.append(el('summary','Source freshness & provenance'));body.append(freshness);const clocks=addDetailSection(freshness,'Source freshness');clocks.append(el('p','Jira record updated: '+timestampDescription(d.freshness.jira)),el('p',(d.freshness.worker_basis==='linked_task_updated_at_proxy'?'Worker report proxy (linked task updated): ':'Worker report: ')+timestampDescription(d.freshness.worker)),el('p','Evidence recorded: '+timestampDescription(d.freshness.evidence)),el('p','Dashboard read: '+timestampDescription(data.generated_at)),el('p','Read time does not update source evidence or establish session liveness.'),el('p','Source: '+d.source));
}
function openIssue(key) {$('issue-detail').dataset.issueKey=key;detailReturnFocus=document.activeElement;renderIssueDetail(key);if(!$('issue-detail').open)$('issue-detail').showModal();$('close-detail').focus();}
function closeIssue() {$('issue-detail').close();if(detailReturnFocus?.isConnected)detailReturnFocus.focus();}
function comparisonKey(s) {return 'mailbox-process-v1:'+JSON.stringify({mode:data.mode,p:s.p,e:s.e,task:s.taskId,team:s.team,q:s.q,...(s.hideCompleted?{}:{completed:'all'})});}
function comparisonMembership(values) {return [...new Set((Array.isArray(values)?values:[]).filter(value=>typeof value==='string'&&value.trim()).map(value=>value.trim()))].sort();}
function comparisonScopeLabel(scope) {return Object.entries(scope).map(([kind,values])=>kind+': '+(values.join(', ')||'none recorded')).join('; ');}
function snapshotForComparison(s) {
 const records={};s.scopedIssues.forEach(i=>{const d=getDelivery(i);records['issue:'+i.key]={kind:'Jira issue',label:i.key+' · '+i.title,stage:d.stage,complete:i.verified===true,blocked:d.blocked,blockers:[...d.blockers].sort(),scope:{projects:comparisonMembership(i.projects),epics:comparisonMembership(i.epic_keys),teams:comparisonMembership(i.teams)}};});
 s.scopedTasks.forEach(t=>{const blockers=Array.isArray(t.blockers)?t.blockers:[];records['task:'+t.id]={kind:'Local task',label:t.id+' · '+taskDisplayTitle(t),stage:t.status||'unknown',complete:t.status==='done'&&t.evidenced===true,blocked:t.status==='blocked'||blockers.length>0,blockers:[...blockers].sort(),scope:{projects:comparisonMembership([t.project]),epics:comparisonMembership(t.epic_keys||[t.epic_key]),issues:comparisonMembership(t.issue_keys),teams:comparisonMembership(t.teams)}};});
 return {version:4,read_at:data.generated_at,records};
}
function validComparisonSnapshot(value) {return value?.version===4&&value.records&&typeof value.records==='object'&&!Array.isArray(value.records)&&Object.values(value.records).every(record=>record&&typeof record.label==='string'&&typeof record.kind==='string'&&typeof record.stage==='string'&&typeof record.complete==='boolean'&&typeof record.blocked==='boolean'&&Array.isArray(record.blockers)&&record.blockers.every(item=>typeof item==='string')&&record.scope&&typeof record.scope==='object'&&!Array.isArray(record.scope)&&['projects','epics','teams',...(record.kind==='Local task'?['issues']:[])].every(key=>Array.isArray(record.scope[key])&&record.scope[key].every(item=>typeof item==='string')));}
function snapshotChanges(previous,current) {
 const result=[];Object.entries(current.records).forEach(([key,record])=>{const old=previous.records[key],label=record.kind+' '+record.label;if(!old){result.push('Added '+label);return;}if(old.stage!==record.stage)result.push(label+': stage / execution '+old.stage+' → '+record.stage);if(old.complete!==record.complete)result.push(label+': verified completion '+(record.complete?'recorded':'removed'));if(old.blocked!==record.blocked||JSON.stringify(old.blockers)!==JSON.stringify(record.blockers))result.push(label+': blockers changed from '+(old.blockers.join('; ')||'none recorded')+' to '+(record.blockers.join('; ')||'none recorded'));if(JSON.stringify(old.scope)!==JSON.stringify(record.scope))result.push(label+': scope / associations changed from '+comparisonScopeLabel(old.scope)+' to '+comparisonScopeLabel(record.scope));});Object.entries(previous.records).forEach(([key,record])=>{if(!Object.hasOwn(current.records,key))result.push('Removed '+record.kind+' '+record.label);});return result;
}
function captureComparison(s) {
 const key=comparisonKey(s),current=snapshotForComparison(s);try{const raw=localStorage.getItem(key);if(!raw)comparison={key,message:'First visit to this filtered scope. The current snapshot is saved for the next successful refresh.',changes:[]};else{let previous;try{previous=JSON.parse(raw);}catch(_){previous=null;}comparison=validComparisonSnapshot(previous)?{key,message:'Compared with this scope last viewed at '+previous.read_at,changes:snapshotChanges(previous,current)}:{key,message:'Saved comparison is unavailable or invalid. A new baseline is saved for the next refresh.',changes:[]};}localStorage.setItem(key,JSON.stringify(current));}catch(_){comparison={key,message:'Browser storage unavailable; changes since the last view cannot be persisted.',changes:[]};}
}
function renderChanges(s) {const area=$('changes-since-view');area.replaceChildren();if(!comparison||comparison.key!==comparisonKey(s)){area.append(el('p','Refresh data to compare this filtered scope. Switching filters, tabs or focus does not advance its baseline.'));return;}area.append(el('p',comparison.message));comparison.changes.forEach(change=>area.append(el('p',change,'scope-change')));if(!comparison.changes.length&&comparison.message.startsWith('Compared'))area.append(el('p','No issue or local task additions, removals, stage, blocker, verified-completion or scope / association changes.'));}
function focusGraph(graph,view,id,mode,descendantsOnly=false,hiddenIds=new Set()) {
 if(!id)return view;if(!graph.nodes.some(n=>n.id===id))return {nodes:[],edges:[],focusMissing:true};
 const edges=graph.edges.filter(e=>e.kind===(mode==='hierarchy'?'membership':'requires')),visible=new Set([id]);
 for(const direction of descendantsOnly?['descendants']:['ancestors','descendants']){const seen=new Set([id]),queue=[id];for(let index=0;index<queue.length;index++){const current=queue[index];edges.forEach(e=>{const next=direction==='ancestors'?(e.to===current?e.from:null):(e.from===current?e.to:null);if(next&&!seen.has(next)){seen.add(next);visible.add(next);queue.push(next);}});}}
 hiddenIds.forEach(hidden=>{if(hidden!==id)visible.delete(hidden);});
 const selected=new Set(view.nodes.filter(n=>!n.boundary).map(n=>n.id));return {nodes:graph.nodes.filter(n=>visible.has(n.id)).map(n=>({...n,boundary:!selected.has(n.id)})),edges:edges.filter(e=>visible.has(e.from)&&visible.has(e.to))};
}
function renderMetrics(s) {
 const m=s.totals,area=$('metrics');area.replaceChildren();
 $('scope-estimate').textContent=`Known-scope estimate: ${range(m.estimate)} (low / likely / high). ${m.scope_complete?'Enumerated imported scope.':'Partial or unavailable scope; full total unknown.'}`;
 const entries=[['Verified progress',m.percent===null?'—':m.percent+'%',`${m.verified_issues} verified / ${m.known_issues} known issues${m.percent===null?' · scope unavailable or incomplete':''}`],['Known issues',String(m.known_issues),`${s.tasks.length} local tasks · ${s.epics.length} epics`],['Likely remaining',m.remaining_estimate?moneyless(m.remaining_estimate.likely)+' h':'—',m.remaining_estimate?`Low ${moneyless(m.remaining_estimate.low)} · High ${moneyless(m.remaining_estimate.high)} · ${m.scope_complete?'enumerated scope':'known subset'}`:'No enumerated work to estimate'],['Measured agent time',m.actual_machine_hours===null?'—':moneyless(m.actual_machine_hours)+' h',`${m.measured_issues} / ${m.known_issues} issues measured · ${m.measured_issues===0?'no measured actuals':m.measured_issues<m.known_issues?'known subset only':'recorded actuals'}`]];
 $('estimate-summary').replaceChildren();
 [['Known-scope estimate',range(m.estimate),'Low / likely / high · '+(m.scope_complete?'enumerated scope':'known subset only')],['Remaining estimate',range(m.remaining_estimate),'Excludes verified issues'],['Measured actual',m.actual_machine_hours===null?'Unavailable':moneyless(m.actual_machine_hours)+' h',`${m.measured_issues} / ${m.known_issues} issues measured`]].forEach(([title,value,detail])=>{const card=el('article',undefined,'metric');card.append(el('span',title,'label'),el('strong',value),el('small',detail));$('estimate-summary').append(card);});
 entries.forEach(([label,value,detail])=>{const card=el('article',undefined,'metric');card.append(el('span',label,'label'),el('strong',value),el('small',detail));area.append(card);});
}
function renderProjects(s) {
 const area=$('projects');area.replaceChildren();
 const projects=s.projects;
 $('project-count').textContent=`${projects.length} projects · dynamic inventory`;
 projects.forEach(p=>{const card=el('button',undefined,'project-card'+(s.p===p.id?' selected':''));card.type='button';card.setAttribute('aria-label',`Filter ${p.name}`);
 const top=el('div',undefined,'project-top');top.append(el('h3',p.name),el('span',p.kind==='coordination'?'Coordination':p.registered?'Registered':'Unregistered','tag'));card.append(top);
 const selected=s.scopedIssues.filter(i=>i.projects.includes(p.id));
 const m=sumMetrics(selected,s.totals.scope_complete),track=el('div',undefined,'progress-track'),fill=el('div',undefined,'progress-fill');fill.style.width=(m.percent||0)+'%';track.append(fill);card.append(track);
 card.append(el('strong',m.percent===null?'No verified percentage':m.percent+'% verified'));
 card.append(el('p',`${selected.length} known issues · ${s.scopedTasks.filter(t=>t.project===p.id).length} local tasks`));
 let allocated={low:0,likely:0,high:0};selected.forEach(i=>{const estimate=issueEstimate(i);Object.keys(allocated).forEach(k=>allocated[k]+=estimate[k]/i.projects.length);});
 card.append(el('p',`Allocated model: ${range(selected.length?allocated:null)}`));
 card.append(el('p',selected.length?(m.scope_complete?'Enumerated scope · low / likely / high':'Known subset only · full estimate unknown'):'No imported issue scope'));
 if(!s.team&&p.reported_estimate)card.append(el('p','Reported project estimate: '+range(p.reported_estimate)));
 if(!s.team&&p.reported_actual_machine_hours!==null)card.append(el('p','Reported project actual: '+moneyless(p.reported_actual_machine_hours)+' h'));
 card.addEventListener('click',()=>{$('project').value=s.p===p.id?'':p.id;writeUrl();render();});area.append(card);});
 if(!projects.length)empty(area,'No matching projects','Choose another filter or add project statistics to the mailbox.');
}
function svgEl(tag,attrs={},text) {const n=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));if(text!==undefined)n.textContent=text;return n;}
function cycleEdges(edges) {
 const adjacency=new Map();edges.forEach(e=>{if(!adjacency.has(e.from))adjacency.set(e.from,[]);adjacency.get(e.from).push(e.to);});
 const reaches=(start,target)=>{const queue=[start],seen=new Set();while(queue.length){const n=queue.pop();if(n===target)return true;if(seen.has(n))continue;seen.add(n);queue.push(...(adjacency.get(n)||[]));}return false;};
 return new Set(edges.filter(e=>reaches(e.to,e.from)).map(e=>e.from+'>'+e.to));
}
function selectGraph(graph,s,mode,includeLocal=true) {
 const selected=new Set(),issueIds=new Set(s.issues.map(i=>'issue:'+i.key)),taskIds=new Set(s.tasks.map(t=>'task:'+t.id));
 const projectIds=new Set(s.projects.map(p=>'project:'+p.id)),epicIds=new Set(s.epics.map(e=>'epic:'+e.key));
 graph.nodes.forEach(n=>{
  const contextual=n.contextual===true||n.relationship_direction==='unknown'||n.status==='relationship direction unknown';
  const contextVisible=contextual&&s.epics.some(e=>(n.epic_keys||[]).includes(e.key));
  const anchor=mode==='execution'&&(epicIds.has(n.id)||projectIds.has(n.id))&&graph.edges.some(e=>e.kind==='requires'&&(e.from===n.id||e.to===n.id));
  const milestone=n.kind==='milestone'&&(!s.p||(n.projects||[]).includes(s.p))&&(!s.e||(n.epic_keys||[]).includes(s.e))&&matchesTeam(n,s.team)&&(!s.q||(n.id+' '+n.label+' '+(n.detail||'')).toLowerCase().includes(s.q));
  if(issueIds.has(n.id)||((includeLocal||isOpenFreeTextTask(n))&&taskIds.has(n.id))||milestone||contextVisible||anchor||(mode==='hierarchy'&&(projectIds.has(n.id)||epicIds.has(n.id))))selected.add(n.id);
 });
 const edges=graph.edges.filter(e=>e.kind===(mode==='hierarchy'?'membership':'requires'));
 const visible=new Set(selected);
 // Ancestors stay visible through every level, even when a filter hides their owner.
 let changed=true;while(changed){changed=false;edges.forEach(e=>{if(visible.has(e.to)&&!visible.has(e.from)){visible.add(e.from);changed=true;}});}
 // Show direct consumers as context without pulling their entire unrelated work into scope.
 if(mode==='execution')edges.forEach(e=>{if(selected.has(e.from)&&(includeLocal||!e.to.startsWith('task:')))visible.add(e.to);});
 return {nodes:graph.nodes.filter(n=>visible.has(n.id)).map(n=>({...n,boundary:!selected.has(n.id)||n.contextual===true||n.relationship_direction==='unknown'||n.status==='relationship direction unknown'})),edges:edges.filter(e=>visible.has(e.from)&&visible.has(e.to))};
}
function layoutGraph(nodes,edges) {
 const cycles=cycleEdges(edges),ids=new Set(nodes.map(n=>n.id)),depth=new Map(nodes.map(n=>[n.id,0]));
 const acyclic=edges.filter(e=>ids.has(e.from)&&ids.has(e.to)&&!cycles.has(e.from+'>'+e.to));
 const incoming=new Map(nodes.map(n=>[n.id,0])),outgoing=new Map();
 acyclic.forEach(e=>{incoming.set(e.to,incoming.get(e.to)+1);if(!outgoing.has(e.from))outgoing.set(e.from,[]);outgoing.get(e.from).push(e.to);});
 const queue=nodes.filter(n=>incoming.get(n.id)===0).map(n=>n.id);
 for(let i=0;i<queue.length;i++){const id=queue[i];(outgoing.get(id)||[]).forEach(next=>{depth.set(next,Math.max(depth.get(next),depth.get(id)+1));incoming.set(next,incoming.get(next)-1);if(incoming.get(next)===0)queue.push(next);});}
 const rows=new Map(),positions=new Map();
 [...nodes].sort((a,b)=>String(a.label||a.id).localeCompare(String(b.label||b.id))).forEach(n=>{const rank=depth.get(n.id),row=rows.get(rank)||0;positions.set(n.id,{x:20+rank*285,y:48+row*170});rows.set(rank,row+1);});
 return {positions,cycles,width:Math.max(760,(Math.max(0,...depth.values())+1)*285+40),height:Math.max(230,Math.max(0,...rows.values())*170+65)};
}
function graphCompletion(n) {return n.unresolved?'Unresolved record':n.ready?'Verified complete':'Completion unverified';}
function graphMarker(n,unmet,process=null) {
 if(n.ready)return {label:'✓ Verified complete',kind:'complete'};
 if(n.kind==='issue'&&process)return process;
 if(n.status==='blocked')return {label:'! Blocked',kind:'blocked'};
 if(n.status==='in_progress')return {label:'● In progress',kind:'progress'};
 if(unmet)return {label:'! Prerequisites unmet',kind:'blocked'};
 return {label:'○ Unverified',kind:'unknown'};
}
function taskAssociation(record) {
 const linked=Array.isArray(record.issue_keys)?record.issue_keys:[];
 if(linked.length)return 'Linked Jira: '+linked.join(', ');
 if(record.mapping_kind==='epic'&&(record.epic_keys||[]).length)return 'Epic scope: '+record.epic_keys.join(', ');
 return 'No Jira mapping recorded';
}
function taskDisplayTitle(record) {
 const raw=String(record.title||record.label||'Untitled local work');
 const keys=[...(Array.isArray(record.issue_keys)?record.issue_keys:[]),...(record.mapping_kind?(record.epic_keys||[]):[])].sort((a,b)=>b.length-a.length);
 const key=keys.find(k=>raw.startsWith(k)&&(raw.length===k.length||/[\s:—–-]/.test(raw[k.length])));
 return key?raw.slice(key.length).replace(/^[\s:—–-]+/,'')||'Untitled local work':raw;
}
function graphIdentity(n) {
 if(n.kind==='task')return {identity:'Local task · '+n.id.replace(/^task:/,'').slice(0,8),title:taskDisplayTitle(n),association:taskAssociation(n),kind:'Local task'};
 const type=String(n.issue_type||n.source_issue_type||'');
 const kind=n.kind==='issue'?(n.unresolved&&!/^[A-Z][A-Z0-9_]*-\d+$/.test(n.id.replace(/^issue:/,'')))?'External reference':/sub.?task/i.test(type)?'Jira subtask':type.toLowerCase()==='story'?'Jira story':'Jira issue':n.kind==='epic'?type?'Jira '+type:'Epic scope':n.kind==='milestone'?'Evidence milestone':'Project';
 return {identity:n.label||n.id,title:n.detail||kind,association:(n.epic_keys||[]).length?'Epic scope: '+n.epic_keys.join(', '):n.kind==='project'?'Project grouping':'No Epic mapping recorded',kind};
}
function graphRecordLabel(n) {const info=graphIdentity(n);return info.identity+(n.kind==='task'?' · '+info.title:'');}
function localWorkGroups(graph,s) {
 const selected=new Set(s.tasks.map(t=>'task:'+t.id)),owners=new Map([...s.issues.map(i=>[i.key,i]),...s.epics.map(e=>[e.key,e])]);
 const groups={history:[],current:[],audit:[]};
 graph.nodes.filter(n=>n.kind==='task'&&selected.has(n.id)).forEach(n=>{
  if(n.ready){groups.history.push(n);return;}
  const keys=(n.issue_keys||[]).length?n.issue_keys:n.mapping_kind==='epic'?(n.epic_keys||[]):[];
  const active=isOpenFreeTextTask(n)||keys.some(key=>{const owner=owners.get(key);return owner&&['in progress','development peer review'].includes(String(owner.source_status||'').toLowerCase());});
  groups[active?'current':'audit'].push(n);
 });
 return groups;
}
function membershipDescendants(id) {
 const seen=new Set(),queue=[id];
 for(let index=0;index<queue.length;index++)data.graph.edges.forEach(e=>{if(e.kind==='membership'&&e.from===queue[index]&&!seen.has(e.to)&&e.to!==id){seen.add(e.to);queue.push(e.to);}});
 return seen;
}
function showSubtree(id) {focusId=id;focusSubtree=true;$('graph-hierarchy').setAttribute('aria-pressed','true');$('graph-execution').setAttribute('aria-pressed','false');writeUrl();renderMap(selection());}
function renderLocalWork(groups) {
 [['current','Current local work','Owning Jira work is recorded as active, or the task is an open free-text task without Jira. Expand a local record for its result and evidence.'],['history','Local work history','Only local tasks with verified completion evidence appear here; completion does not imply the owning Jira item is complete.'],['audit','Other local work / audit','Owning Jira activity is unknown or not recorded as active. These records remain inspectable; unverified prerequisites stay on the map.']].forEach(([kind,title,note])=>{
  const area=$('local-work-'+kind);area.replaceChildren();area.append(el('summary',`${title} · ${groups[kind].length}`),el('p',note,'footnote'));
  groups[kind].forEach(n=>{const info=graphIdentity(n),marker=graphMarker(n,(n.unmet_prerequisites||[]).length),detail=el('details',undefined,'graph-record');detail.dataset.nodeId=n.id;const summary=el('summary');summary.append(el('span',marker.label,'graph-status marker-'+marker.kind),document.createTextNode(' '+info.identity+' · '+info.title));detail.append(summary,el('p',info.association),el('p','Full local task ID: '+n.id.replace(/^task:/,'')),el('p','Full recorded title: '+n.label),el('p','Result: '+graphCompletion(n)+' · Recorded status: '+(labels[n.status]||n.status||'Unknown')),el('p','Source: '+(n.source||'Source unavailable')));
   (n.evidence||[]).forEach(item=>detail.append(el('p','Evidence: '+item)));area.append(detail);
  });
 });
}
function renderMap(s) {
 const area=$('map'),list=$('dependency-list'),records=$('dependency-nodes');area.replaceChildren();list.replaceChildren();records.replaceChildren();renderLocalWork({current:[],history:[],audit:[]});
 const mode=$('graph-hierarchy').getAttribute('aria-pressed')==='true'?'hierarchy':'execution';
 $('graph-legend').textContent=mode==='hierarchy'?'Membership only: project → Epic → parent issue → subtask → linked local work. Membership does not impose execution order.':'Prerequisite → dependent. Markers: green ✓ verified complete · blue ● in progress · orange ! blocked / unmet · gray ○ unverified. Dashed: context / unresolved · coral edges: cycle.';
 if(!data.graph){empty(area,'Relationship data unavailable','Refresh after updating the dashboard projection. No dependencies are inferred from task titles or blockers.');$('graph-summary').textContent='No graph snapshot available.';return;}
 const showLocal=$('graph-local-work').checked,groups=localWorkGroups(data.graph,s);renderLocalWork(groups);
 const baseView=selectGraph(data.graph,s,mode,showLocal),hiddenIds=s.hideCompleted?settledCompletedNodeIds():new Set(),rawView=s.invalid?.length?{nodes:[],edges:[]}:focusGraph(data.graph,baseView,focusId,mode,focusSubtree,hiddenIds),kept=new Set(rawView.nodes.filter(n=>showLocal||focusSubtree||n.kind!=='task'||!n.ready).map(n=>n.id));
 const view={nodes:rawView.nodes.filter(n=>kept.has(n.id)),edges:rawView.edges.filter(e=>kept.has(e.from)&&kept.has(e.to))},selected=view.nodes.filter(n=>!n.boundary),boundary=view.nodes.length-selected.length;
 $('focus-summary').textContent=focusId?(rawView.focusMissing?'Focus unavailable: '+focusId:(focusSubtree?'Showing every linked descendant of '+focusId+' (children, their children and local tasks)':'Focused on '+focusId+' · prerequisites and dependents')+'; outside-filter context does not change totals.'):'No graph focus · showing filtered scope';$('clear-focus').hidden=!focusId;
 const connected=new Set(view.edges.flatMap(e=>[e.from,e.to]));
 const drawing=mode==='execution'?view.nodes.filter(n=>connected.has(n.id)||n.id===focusId||isOpenFreeTextTask(n)):view.nodes;
 const freeTextShown=view.nodes.filter(isOpenFreeTextTask).length;
 $('graph-summary').textContent=`${selected.length} selected records · ${boundary} context records · ${view.edges.length} ${mode==='hierarchy'?'membership links':'prerequisites'}. Context does not change scope totals. ${showLocal?'All local work shown.':`${groups.history.length} verified local tasks in history; ${freeTextShown} open free-text tasks shown; ${groups.current.length+groups.audit.length-freeTextShown} other local records folded. Unverified prerequisite context stays visible.`}`;
 if(!drawing.length)empty(area,rawView.focusMissing?'Focused record unavailable':mode==='execution'?'No recorded prerequisites in this selection':'No hierarchy records in this selection',mode==='execution'?'An empty prerequisite list does not establish readiness or completion.':'Choose another filter or import immediate issue parent relationships.');
 else {
  const layout=layoutGraph(drawing,view.edges),svg=svgEl('svg',{width:layout.width,height:layout.height,viewBox:`0 0 ${layout.width} ${layout.height}`,role:'img','aria-label':mode==='hierarchy'?'Issue parent hierarchy':'Layered execution prerequisite map'});
  const defs=svgEl('defs');for(const [id,color] of [['graph-arrow','#4a7bc3'],['graph-unmet','#bd8739'],['graph-cycle','#d7745e']]){const marker=svgEl('marker',{id,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});marker.append(svgEl('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:color}));defs.append(marker);}svg.append(defs);
  const byId=new Map(view.nodes.map(n=>[n.id,n]));
  view.edges.forEach(e=>{const a=layout.positions.get(e.from),b=layout.positions.get(e.to);if(!a||!b)return;const cyclic=layout.cycles.has(e.from+'>'+e.to),met=byId.get(e.from)?.ready===true;
   const color=e.kind==='membership'?'#d6e0eb':cyclic?'#d7745e':met?'#4a7bc3':'#bd8739';
   const path=b.x>a.x?`M${a.x+245} ${a.y+34} C${a.x+265} ${a.y+34} ${b.x-20} ${b.y+34} ${b.x} ${b.y+34}`:`M${a.x+245} ${a.y+34} C${a.x+275} ${a.y+34} ${b.x+275} ${b.y+34} ${b.x+245} ${b.y+34}`;
   const line=svgEl('path',{d:path,stroke:color,'stroke-width':1.8,fill:'none','stroke-dasharray':e.unresolved||byId.get(e.from)?.boundary||byId.get(e.to)?.boundary?'5 4':'none','data-from':e.from,'data-to':e.to,'data-kind':e.kind});
   if(e.kind==='requires')line.setAttribute('marker-end',`url(#${cyclic?'graph-cycle':met?'graph-arrow':'graph-unmet'})`);svg.append(line);
  });
  const ranks=new Set([...layout.positions.values()].map(p=>p.x));[...ranks].sort((a,b)=>a-b).forEach((x,i)=>svg.append(svgEl('text',{x,y:25,class:'lane'},`${mode==='hierarchy'?'LEVEL':'STEP'} ${i+1}`)));
  drawing.forEach(n=>{const p=layout.positions.get(n.id),g=svgEl('g',{'data-node-id':n.id,'data-boundary':String(n.boundary)});
   const info=graphIdentity(n),unmet=data.graph.edges.filter(e=>e.kind==='requires'&&e.to===n.id&&!data.graph.nodes.find(x=>x.id===e.from)?.ready).length,issue=n.kind==='issue'?data.issues.find(i=>'issue:'+i.key===n.id):null,marker=graphMarker(n,unmet,issue?stageMarker(issue):null);
   g.append(svgEl('title',{},`${info.identity} — ${info.title} · ${info.kind} · ${info.association} · Full record: ${n.label}${n.detail?' — '+n.detail:''} · ${n.id} · ${graphCompletion(n)}${n.boundary?' · outside selection context':''}`));
   g.append(svgEl('rect',{x:p.x,y:p.y,width:245,height:150,rx:7,stroke:n.ready?'#8ecab5':'#d5e0ec',fill:'#fff','stroke-dasharray':n.boundary||n.unresolved?'4 3':'none'}));
   g.append(svgEl('text',{x:p.x+10,y:p.y+19,'font-weight':600},String(info.identity).slice(0,34)),svgEl('text',{x:p.x+10,y:p.y+38},String(info.title).slice(0,34)),svgEl('text',{x:p.x+10,y:p.y+57,'font-size':10},`${info.kind}${n.boundary?' · context':''}${n.unresolved?' · unresolved':''} · ${info.association}`.slice(0,46)),svgEl('text',{x:p.x+10,y:p.y+75,'font-size':11,'font-weight':600,class:'node-time'},issue?`Estimate ${recordedHours(issue.original_estimate_hours,'—')} · Worked ${recordedHours(issue.worked_hours,'—')}`:''));
   const badge=svgEl('g',{class:'graph-marker marker-'+marker.kind,'data-marker':marker.kind});badge.append(svgEl('rect',{x:p.x+9,y:p.y+84,width:225,height:22,rx:4}),svgEl('text',{x:p.x+16,y:p.y+99,'font-size':11,'font-weight':600},marker.label));g.append(badge);
   const blocked=issue&&getDelivery(issue).blocked,bottom=svgEl('g',blocked?{class:'node-blocked'}:{});if(blocked)bottom.append(svgEl('rect',{x:p.x+9,y:p.y+111,width:225,height:18,rx:3}));bottom.append(svgEl('text',{x:p.x+13,y:p.y+123,'font-size':10},`${blocked?'! Blocked · ':''}Unmet prerequisites: ${unmet}`));g.append(bottom);const descendants=membershipDescendants(n.id).size;if(descendants){const subtree=svgEl('g',{class:'node-subtree-button',role:'button',tabindex:'0','aria-label':`Show ${descendants} linked descendant${descendants===1?'':'s'} of ${info.identity}`});subtree.append(svgEl('title',{},'Show this record as a parent with all linked child work in depth'),svgEl('rect',{x:p.x+9,y:p.y+129,width:225,height:16,rx:3}),svgEl('text',{x:p.x+15,y:p.y+141,'font-size':10,'font-weight':600},`⤵ Show ${descendants} linked descendant${descendants===1?'':'s'}`));const open=event=>{event.stopPropagation();showSubtree(n.id);};subtree.addEventListener('click',open);subtree.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();open(event);}});g.append(subtree);}if(issue){g.setAttribute('role','button');g.setAttribute('tabindex','0');g.setAttribute('aria-label','Open '+issue.key+' details');g.addEventListener('click',()=>openIssue(issue.key));g.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();openIssue(issue.key);}});}svg.append(g);
  });area.append(svg);
 }
 const byId=new Map(view.nodes.map(n=>[n.id,n])),cycles=cycleEdges(view.edges);
 view.edges.forEach(e=>{const from=byId.get(e.from),to=byId.get(e.to),cyclic=cycles.has(e.from+'>'+e.to),met=from?.ready===true;
  const detail=el('details',undefined,'graph-relation'+(cyclic?' cycle':''));detail.append(el('summary',`${from?graphRecordLabel(from):e.from} → ${to?graphRecordLabel(to):e.to} · ${e.kind==='membership'?'membership':cyclic?'cycle — prerequisite unresolved':met?'prerequisite verified':'prerequisite unmet / unverified'}`));
  detail.append(el('p',`${e.from} → ${e.to}`));if(e.condition)detail.append(el('p','Condition: '+e.condition));
  detail.append(el('p','Source: '+(e.source||'Source unavailable')));if(e.updated_at)detail.append(el('p','Recorded: '+e.updated_at));
  (e.evidence||[]).forEach(item=>detail.append(el('p','Evidence: '+item)));list.append(detail);
 });
 view.nodes.forEach(n=>{const detail=el('details',undefined,'graph-record');detail.dataset.nodeId=n.id;
  const incoming=data.graph.edges.filter(e=>e.kind==='requires'&&e.to===n.id),unmet=incoming.filter(e=>!data.graph.nodes.find(x=>x.id===e.from)?.ready);
  const scheduling=incoming.length?`${unmet.length} / ${incoming.length} prerequisites unmet or unverified`:'No recorded prerequisites; readiness not established';
  const info=graphIdentity(n),issue=n.kind==='issue'?data.issues.find(i=>'issue:'+i.key===n.id):null,marker=graphMarker(n,unmet.length,issue?stageMarker(issue):null),summary=el('summary');summary.append(el('span',marker.label,'graph-status marker-'+marker.kind),document.createTextNode(' '+graphRecordLabel(n)+(n.boundary?' · outside selection context':'')));detail.append(summary,el('p',n.id),el('p',info.kind+' · '+info.association),el('p',info.title),el('p','Full recorded title: '+(n.label||n.id)+(n.detail?' — '+n.detail:'')),el('p',`Recorded status: ${labels[n.status]||n.status||'Unknown'} · ${scheduling}`));
  unmet.forEach(e=>detail.append(el('p','Awaiting: '+(data.graph.nodes.find(x=>x.id===e.from)?graphRecordLabel(data.graph.nodes.find(x=>x.id===e.from)):e.from))));
  detail.append(el('p','Source: '+(n.source||'Source unavailable')),el('p','Recorded: '+(n.updated_at||'Timestamp unavailable')));
  (n.evidence||[]).forEach(item=>detail.append(el('p','Evidence: '+item)));records.append(detail);
 });
}
function executionSummary(tasks) {
 const counts={};tasks.forEach(t=>{counts[t.status]=(counts[t.status]||0)+1;});
 return Object.entries(counts).map(([key,value])=>`${value} ${labels[key]||key}`).join(' · ')||'No linked tasks';
}
function renderExecution(s) {
 const area=$('execution-summary');area.replaceChildren();
 const linked=s.tasks.filter(t=>t.issue_keys.length),epicOnly=s.tasks.filter(t=>t.mapping_kind==='epic');
 const unresolved=s.tasks.filter(t=>t.mapping_kind==='unresolved'),unmapped=s.tasks.filter(t=>t.mapping_kind==='unmapped');
 area.append(el('p',`${s.tasks.length} local tasks in selection: ${linked.length} linked to issues · ${epicOnly.length} Epic-level · ${unmapped.length} without mapping. Mapping warnings: ${unresolved.length} tasks with unresolved keys (may also have valid issue links)`));
 area.append(el('p',`${executionSummary(s.tasks)}. Issue verification remains separate.`, 'muted'));
 const cards=$('epic-work');cards.replaceChildren();
 s.epics.forEach(epic=>{
  const tasks=epicOnly.filter(t=>t.epic_keys.includes(epic.key));if(!tasks.length)return;
  const card=el('article',undefined,'task');card.append(el('strong',`${epic.key} · Epic-level work`),el('p',executionSummary(tasks)),el('p','These tasks are not allocated to individual issues.'));
  const details=el('details');details.append(el('summary',`${tasks.length} tasks · show work`));tasks.forEach(t=>details.append(el('p',`Local task · ${t.id.slice(0,8)} · ${labels[t.status]||t.status} · ${taskDisplayTitle(t)} · ${taskAssociation(t)}`)));card.append(details);cards.append(card);
 });
}
function renderIssues(s) {
 const tbody=$('issues');tbody.replaceChildren();$('issue-count').textContent=`${s.issues.length} unique issues`;
 s.issues.forEach(i=>{
  const row=el('tr'),key=el('td');key.append(issueButton(i,i.key),el('span',i.title));
  if(i.conflict)key.append(el('small','Conflicting snapshots — reconcile source'));
  const members=el('td',i.projects.join(', ')||'Unmapped');members.append(el('small','Team: '+((i.teams||[]).join(', ')||'Unassigned')),el('small',i.epic_keys.join(', ')));
  const source=el('td',i.source_status),local=el('td'),process=stageMarker(i);local.append(el('span',process.label,'graph-status marker-'+process.kind),el('small','Issue verification: '),status(i.local_status));if(getDelivery(i).blocked)local.append(el('span','! Blocked','graph-status marker-blocked'));
  if(i.recorded_local_status!==i.local_status)local.append(el('small',`Recorded: ${labels[i.recorded_local_status]||i.recorded_local_status}`));
  const tasks=s.tasks.filter(t=>i.task_ids.includes(t.id));
  local.append(el('small',`Task execution (filtered): ${executionSummary(tasks)}`),el('small',`${tasks.length} visible / ${i.task_ids.length} required task links (issue-wide)`));
  if(!i.task_ids.length)local.append(el('small','No issue-level task mapping recorded'));
  if(i.recorded_local_status==='pending'&&tasks.some(t=>t.status!=='pending'))local.append(el('small','Linked tasks show activity; recorded issue status is Pending.'));
  const details=el('details');details.append(el('summary','Verification & linked work'));
  details.append(el('p',`Issue-wide verification: ${i.verification_reason}`));
  if(i.recorded_verification_reason)details.append(el('p','Recorded verification note: '+i.recorded_verification_reason));
  tasks.forEach(t=>details.append(el('p',`Local task · ${t.id.slice(0,8)} · ${labels[t.status]||t.status} · ${taskDisplayTitle(t)} · ${t.evidenced?'completion evidence recorded':'completion evidence pending'} · ${t.id}`)));
  if(i.task_execution.missing.length)details.append(el('p','Missing task records: '+i.task_execution.missing.join(', ')));
  details.append(el('p',`Issue updated: ${i.updated_at||'Timestamp unavailable'}`),el('p','Sources: '+i.sources.join(', ')));local.append(details);
  const estimate=el('td',range(issueEstimate(i)));estimate.append(el('small',i.estimate?'Explicit estimate':'Model · low / likely / high'),el('strong',issueTimeText(i)));row.append(key,members,source,local,estimate);tbody.append(row);
 });
 if(!s.issues.length){const r=el('tr'),c=el('td');c.colSpan=5;empty(c,'No issues in this view','No imported scope is not zero progress. Add an Epic snapshot or explore the isolated demo.');r.append(c);tbody.append(r);}
}
function renderTasks(s) {
 const area=$('tasks');area.replaceChildren();$('task-count').textContent=`${s.tasks.length} local tasks`;
 s.tasks.forEach(t=>area.append(taskCard(t)));
 if(!s.tasks.length)empty(area,'No local tasks yet','Tasks appear here even before they are mapped to Jira.');
}
function recordedHours(hours,missing) { return hours===null||hours===undefined?missing:moneyless(hours)+' h'; }
function issueTimeText(issue) { return `Original estimate: ${recordedHours(issue.original_estimate_hours,'not recorded')} · Worked so far: ${recordedHours(issue.worked_hours,'not measured')}`; }
function issueTimeLine(issue) { return el('p',issueTimeText(issue),'issue-time'); }
function issueTime(key) {
  const issue=data.issues.find(i=>i.key===key);if(!issue)return null;
  const measured=issue.worked_hours===null||!data.worked_measured_at?'':' · measured '+timestampDescription(data.worked_measured_at);
  return `${key} · ${issueTimeText(issue)}${measured}`;
}
function taskTime(t) {
  const lines=t.issue_keys.map(issueTime).filter(Boolean);
  return (lines.length?lines:['No linked issue · original estimate and worked time unavailable']).map(line=>el('p',line,'task-time'));
}
function taskCard(t) {
  const card=el('article',undefined,'task');card.dataset.status=t.status;
  const mapping=taskAssociation(t);
  card.append(el('p','Local task · '+t.id.slice(0,8),'local-identity'),el('strong',taskDisplayTitle(t)),status(t.status),el('p',`${t.project} · ${(t.teams||[]).join(', ')||'Unassigned'} · ${mapping}`));
  card.append(...taskTime(t));
  if(t.unresolved_issue_keys.length)card.append(el('p','Unresolved keys: '+t.unresolved_issue_keys.join(', ')));
  card.append(el('p',`${t.evidenced?'Completion evidence recorded':'Completion evidence pending'} · task only; issue verification is separate`),el('p',t.id),el('p',`Updated: ${t.updated_at||'Timestamp unavailable'} · ${t.source}`));
  const original=el('details');original.append(el('summary','Original task record'),el('p','Full local task ID: '+t.id),el('p','Full recorded title: '+t.title),el('p',mapping));card.append(original);
  if(t.blockers.length)card.append(el('p','Blockers: '+t.blockers.join(', ')));
  return card;
}
function renderReported(s) {const area=$('reported');area.replaceChildren();const items=s.reported;items.forEach(r=>{const d=el('details');d.append(el('summary',`${r.project||'All projects'} / ${r.epic||'Project snapshot'} · ${r.source}`),el('p',r.updated_at?'Updated '+r.updated_at:'Timestamp unavailable','muted'),el('pre',JSON.stringify(r.values,null,2)));area.append(d);});if(!items.length)empty(area,'No aggregate statistics recorded',s.team?'No explicitly team-scoped aggregates match. Whole-project totals are hidden for team filters.':'This panel preserves reported figures separately from verified issue progress.');}
const settledCheckIntervalMs=30000;
let renderedSelectionKey='';
function selectionKey(s){return [...s.issues.map(i=>'issue:'+i.key),...s.tasks.map(t=>'task:'+t.id)].join('|');}
function rerenderWhenCompletionSettles(){if(data&&$('hide-completed').checked&&selectionKey(selection())!==renderedSelectionKey)render();}
function render(){if(!data)return;const s=selection();renderedSelectionKey=selectionKey(s);renderMetrics(s);renderProjects(s);renderProcess(s);renderChanges(s);renderMap(s);renderExecution(s);renderIssues(s);renderTasks(s);renderReported(s);$('scope-warning').hidden=!s.invalid.length;$('scope-warning').textContent=s.invalid.length?'Unavailable filter selection: '+s.invalid.join('; ')+'. No scope is shown until the selection is corrected.':'';const warnings=[...(data.warnings||[]),...(data.delivery_warnings||[])];$('warnings').replaceChildren(...warnings.map(w=>el('li',w)));$('warning-count').textContent=`(${warnings.length})`;$('warnings-panel').open=!!warnings.length;$('demo-banner').hidden=data.mode!=='demo';$('freshness').textContent=`${data.mode==='demo'?'Demo':'Mailbox'} read ${timestampDescription(data.generated_at)}`;scenario();}
function selectValue(id,value){const select=$(id);if(value&&![...select.options].some(option=>option.value===value)){const option=el('option','Unavailable: '+value);option.value=value;select.append(option);}select.value=value;}
function populate(id,items,key,label,all){const select=$(id),previous=select.value;select.replaceChildren();const option=el('option',all);option.value='';select.append(option);items.forEach(item=>{const o=el('option',item[label]);o.value=item[key];select.append(o);});selectValue(id,previous);}
const refreshMinimumMs=400,refreshDoneMs=2500;let refreshDoneTimer;
function showRefreshing(){clearTimeout(refreshDoneTimer);const button=$('refresh');button.disabled=true;button.classList.remove('refresh-done');button.classList.add('refreshing');button.setAttribute('aria-busy','true');$('refresh-label').textContent='Refreshing…';$('refresh-status').textContent='Refreshing data';}
function showRefreshed(succeeded){const button=$('refresh');button.disabled=false;button.classList.remove('refreshing');button.removeAttribute('aria-busy');if(!succeeded){$('refresh-label').textContent='Refresh data';$('refresh-status').textContent='Refresh failed';return;}const time=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});button.classList.add('refresh-done');$('refresh-label').textContent='Refreshed '+time;$('refresh-status').textContent='Data refreshed at '+time;refreshDoneTimer=setTimeout(()=>{button.classList.remove('refresh-done');$('refresh-label').textContent='Refresh data';},refreshDoneMs);}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const refreshFailureReasons={unreachable:'the dashboard server could not be reached',unreadable:'the server response could not be read',display:'the snapshot could not be displayed',mailbox:'Unable to read mailbox'};
class RefreshFailure extends Error {}
async function fetchDashboard(params) {
 let response;try{response=await fetch('/api/dashboard?'+params);}catch(_){throw new RefreshFailure(refreshFailureReasons.unreachable);}
 let payload;try{payload=await response.json();}catch(_){throw new RefreshFailure(refreshFailureReasons.unreadable);}
 if(!response.ok)throw new RefreshFailure(payload?.error?.message||refreshFailureReasons.mailbox);
 return payload;
}
function refreshFailureReason(failure) {return failure instanceof RefreshFailure?failure.message:refreshFailureReasons.display;}
async function load(){const ticket=++request;$('error').hidden=true;showRefreshing();const started=Date.now();let succeeded=false;const params=new URLSearchParams({mode:$('demo').checked?'demo':'live'});['low','likely','high','review_multiplier'].forEach(k=>params.set(k,$(k).value));try{const payload=await fetchDashboard(params);if(ticket!==request)return;data=payload;populate('project',data.projects,'id','name','All projects');populateWorkFilter();captureComparison(selection());render();if($('issue-detail').open)renderIssueDetail($('issue-detail').dataset.issueKey);succeeded=true;}catch(e){if(ticket!==request)return;$('error').textContent=`Could not refresh: ${refreshFailureReason(e)}. ${data?'Last successful snapshot is still displayed.':'Check the mailbox mount and try again.'}`;$('error').hidden=false;}finally{await pause(Math.max(0,refreshMinimumMs-(Date.now()-started)));if(ticket===request)showRefreshed(succeeded);}}
function restoreUrl(){const params=new URLSearchParams(location.search);urlStateErrors=[];selectValue('project',params.get('project')||'');selectValue('epic',params.get('epic')||'');selectValue('team',params.get('team')||'');$('search').value=params.get('search')||'';focusId=params.get('focus')||'';focusSubtree=Boolean(focusId)&&params.get('subtree')==='1';const mode=params.get('mode')||'live';$('demo').checked=mode==='demo';if(!['live','demo'].includes(mode))urlStateErrors.push('Mode: '+mode);const graph=params.get('graph')||'execution';if(!['hierarchy','execution'].includes(graph))urlStateErrors.push('Graph mode: '+graph);$('graph-hierarchy').setAttribute('aria-pressed',String(graph==='hierarchy'));$('graph-execution').setAttribute('aria-pressed',String(graph!=='hierarchy'));$('graph-local-work').checked=params.get('local')==='all';$('hide-completed').checked=params.get('completed')!=='all';}
function writeUrl(view){const url=new URL(location.href),values={project:$('project').value,epic:$('epic').value,team:$('team').value,search:$('search').value,focus:focusId,subtree:focusId&&focusSubtree?'1':'',mode:$('demo').checked?'demo':'live',graph:$('graph-hierarchy').getAttribute('aria-pressed')==='true'?'hierarchy':'execution',local:$('graph-local-work').checked?'all':'',completed:$('hide-completed').checked?'':'all'};Object.entries(values).forEach(([key,value])=>{if(value)url.searchParams.set(key,value);else url.searchParams.delete(key);});if(view)url.hash=view;urlStateErrors=[];if(url.href!==location.href)history.pushState(null,'',url);}
function scenario(){const count=Number($('scenario-count').value);const model=data?.estimate_method;if(!model)return;$('scenario-result').textContent=Number.isFinite(count)&&count>=0&&count<=10000?range(Object.fromEntries(['low','likely','high'].map(k=>[k,count*model[k]*model.review_multiplier]))):'Enter a count from 0 to 10,000';}
$('graph-local-work').addEventListener('change',()=>{writeUrl();if(data)renderMap(selection());});
['graph-hierarchy','graph-execution'].forEach(id=>$(id).addEventListener('click',()=>{['graph-hierarchy','graph-execution'].forEach(other=>$(other).setAttribute('aria-pressed',String(id===other)));writeUrl();if(data)renderMap(selection());}));
$('clear-focus').addEventListener('click',()=>{focusId='';focusSubtree=false;writeUrl();render();});
$('close-detail').addEventListener('click',closeIssue);$('issue-detail').addEventListener('cancel',event=>{event.preventDefault();closeIssue();});
$('refresh').addEventListener('click',load);$('demo').addEventListener('change',()=>{writeUrl();load();});['team','project','epic','hide-completed'].forEach(id=>$(id).addEventListener('change',()=>{writeUrl();render();}));$('search').addEventListener('input',()=>{writeUrl();render();});$('model-form').addEventListener('submit',e=>{e.preventDefault();load();});$('scenario-count').addEventListener('input',scenario);restoreUrl();load();setInterval(rerenderWhenCompletionSettles,settledCheckIntervalMs);

function syncThemeButton(){const dark=document.documentElement.dataset.theme!=='light';$('theme-toggle').textContent=dark?'☀ Light theme':'☾ Dark theme';$('theme-toggle').setAttribute('aria-label',dark?'Switch to light theme':'Switch to dark theme');}
$('theme-toggle').addEventListener('click',()=>{const next=document.documentElement.dataset.theme==='light'?'dark':'light';document.documentElement.dataset.theme=next;try{localStorage.setItem('mailbox-theme',next);}catch(_){}syncThemeButton();});syncThemeButton();

const viewTitles={overview:['A clear view of the work.','Dependencies, verified progress and agent effort in one place.'],dependencies:['See what depends on what.','Follow project, Epic, issue and local task relationships.'],tasks:['From scope to verified work.','Jira snapshots and local execution, with evidence kept distinct.'],estimates:['Plan the effort explicitly.','Explore agent machine-hours with transparent assumptions.'],sources:['Know where the numbers come from.','Reported snapshots, provenance and data-quality warnings.']};
function selectView(){const requested=location.hash.slice(1);const key=Object.hasOwn(viewTitles,requested)?requested:'overview';document.querySelectorAll('.view-tabs [role=tab]').forEach(tab=>{const active=tab.id==='tab-'+key;tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;});document.querySelectorAll('.page-view').forEach(view=>view.hidden=view.id!=='view-'+key);document.querySelector('.page-heading h1').textContent=viewTitles[key][0];document.querySelector('.page-heading .subtitle').textContent=viewTitles[key][1];document.querySelector('.page-heading .eyebrow').textContent='PROJECT OPERATIONS / '+key.toUpperCase();document.title='Mailbox · '+key[0].toUpperCase()+key.slice(1);}
document.querySelector('.view-tabs').addEventListener('keydown',event=>{const tabs=[...document.querySelectorAll('.view-tabs [role=tab]')],index=tabs.indexOf(document.activeElement);if(index<0)return;let next;if(event.key==='ArrowRight')next=(index+1)%tabs.length;else if(event.key==='ArrowLeft')next=(index+tabs.length-1)%tabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabs.length-1;else return;event.preventDefault();tabs[next].focus();location.hash=tabs[next].getAttribute('href');});
window.addEventListener('hashchange',()=>{selectView();window.scrollTo({top:0});});window.addEventListener('popstate',()=>{const previousMode=$('demo').checked;restoreUrl();selectView();if(!data||previousMode!==$('demo').checked)load();else render();});selectView();
