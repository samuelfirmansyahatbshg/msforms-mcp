// Measured creation protocol adapted from oven-report-generator/scripts/forms_build.js.
// API requests remain inside the authenticated page. Caller supplies an already validated spec.
async ({root, formId, spec, dry=false, append=false}) => {
  const T = {Text:'Question.TextField',Choice:'Question.Choice',Upload:'Question.FileUpload'};
  const INFO_TEXT = {Multiline:false,ShuffleOptions:false,ShowRatingLabel:false};
  const INFO_NUMBER = {...INFO_TEXT,Validation:{rule:0},IsNumber:true,NumberValidationRule:'IsNumber'};
  const INFO_CHOICE = {Choices:[],ChoiceType:1,AllowOtherAnswer:false,OptionDisplayStyle:'ListAll',
    ChoiceRestrictionType:'None',ShuffleOptions:false,ShowRatingLabel:false};
  const INFO_UPLOAD = {HasSpecificFileType:false,FileTypes:{Word:true,Excel:true,PowerPoint:true,
    PDF:true,Image:true,Video:true,Audio:true},MaxFileCount:1,MaxFileSize:10,
    ShuffleOptions:false,ShowRatingLabel:false,IsMathQuiz:false};
  const pause = () => new Promise(r=>setTimeout(r,120));
  const endpoint = `${root}/forms('${formId}')`;
  const read = async () => {
    const r=await window.__formsRequest({url:`${root}/light/forms('${formId}')?$expand=questions,descriptiveQuestions`,method:'GET',body:null});
    if(r.status!==200) throw new Error(`read_http_${r.status}`);
    return r.data;
  };
  const write = async (method,path,body) => {
    await pause();
    const r=await window.__formsRequest({url:endpoint+path,method,body});
    if(![200,201,204].includes(r.status)) throw new Error(`write_http_${r.status}`);
    return r.data;
  };
  const form=await read(), all=FORMS.cards(form);
  if(all.some(c=>['Question','Section'].includes((c.title||'').trim())))
    return {status:'blocked',reason:'untitled_orphan',created:[]};
  // Repeated exact titles claim ordered queues, never the same existing card twice.
  const queues=new Map();
  for(const c of all) {
    const key=c.type+'\0'+c.title;
    if(!queues.has(key)) queues.set(key,[]);
    queues.get(key).push(c);
  }
  const matches=spec.map(s=>append?null:(queues.get((s.s?'Question.ColumnGroup':T[s.t])+'\0'+(s.s||s.q))||[]).shift()||null);
  // Resume only an unchanged prefix: filling earlier holes would change workbook creation order.
  if(!append && (all.length>spec.length || all.some((c,i)=>matches[i]?.id!==c.id) || matches.slice(all.length).some(Boolean)))
    return {status:'blocked',reason:'incompatible_existing_layout',created:[]};
  const existingDrift=append?[]:FORMS.verify(form,spec.slice(0,all.length));
  if(existingDrift.length) return {status:'blocked',reason:'existing_property_drift',mismatches:existingDrift,created:[]};
  const result={status:dry?'planned':'completed',created:[],skipped:append?[]:all.map(c=>c.id),warnings:[],key_ids:{}};
  for(let i=0;i<spec.length;i++) {
    const s=spec[i], title=s.s||s.q;
    if(matches[i]) { if(s.k)result.key_ids[s.k]=matches[i].id; continue; }
    const id='r'+crypto.randomUUID().replaceAll('-',''), collection=s.s?'descriptiveQuestions':'questions';
    const order = (Math.floor(Math.max(0,...all.map(c=>c.order))/1000000) + i-(append?0:all.length)+1)*1000000;
    if(dry) { result.created.push({id:null,title,order}); continue; }
    let qi;
    if(s.t==='Text') qi=s.num?INFO_NUMBER:{...INFO_TEXT,Multiline:s.long!==false};
    if(s.t==='Choice') qi={...INFO_CHOICE,Choices:s.opts.map(v=>({Description:v,FormsProDisplayRTText:v}))};
    if(s.t==='Upload') qi=INFO_UPLOAD;
    try {
      await write('POST','/'+collection,{id,order,type:s.s?'Question.ColumnGroup':T[s.t],
        title:s.s?'Section':'Question',isQuiz:false,required:!s.s,...(qi?{questionInfo:JSON.stringify(qi)}:{})});
      await write('PATCH',`/${collection}('${id}')`,{title,formsProRTQuestionTitle:title});
      if(!s.s&&!s.req) await write('PATCH',`/${collection}('${id}')`,{required:false});
      if(s.t==='Upload'&&s.mb&&s.mb!==10) {
        try { await write('PATCH',`/${collection}('${id}')`,{questionInfo:JSON.stringify({...INFO_UPLOAD,MaxFileSize:s.mb})}); }
        catch(e) { result.warnings.push({id,reason:'file_size_patch_failed'}); }
      }
      result.created.push({id,title,order});
      if(s.k)result.key_ids[s.k]=id;
    } catch(e) {
      result.status='partial'; result.error={id,index:i,reason:String(e.message)}; break;
    }
  }
  if(!dry) {
    const fresh=await read();
    const target=append?{questions:(fresh.questions||[]).filter(c=>result.created.some(x=>x.id===c.id)),
      descriptiveQuestions:(fresh.descriptiveQuestions||[]).filter(c=>result.created.some(x=>x.id===c.id))}:fresh;
    result.mismatches=FORMS.verify(target,spec);
    if(result.mismatches.length) result.status='partial';
    else if(!result.created.length) result.status='already_applied';
  }
  return result;
}
