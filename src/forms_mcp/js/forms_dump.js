// Adapted from oven-report-generator/scripts/forms_dump.js. No clipboard or logging.
(() => {
  const SHORT = {'Question.TextField':'Text','Question.Choice':'Choice',
    'Question.FileUpload':'Upload','Question.Rating':'Rating','Question.DateTime':'Date',
    'Question.Ranking':'Ranking','Question.NPS':'NPS','Question.MatrixChoiceGroup':'Likert',
    'Question.MatrixChoice':'LikertRow','Question.ColumnGroup':'Section'};
  function info(c) { try { return JSON.parse(c.questionInfo || '{}'); } catch (_) { return null; } }
  function cards(f) { return [...(f.questions||[]), ...(f.descriptiveQuestions||[])]
    .sort((a,b)=>(a.order??0)-(b.order??0)); }
  function normalize(f) {
    let section = null;
    return cards(f).map(c => {
      const q = info(c);
      if (c.type === 'Question.ColumnGroup') section = c.id;
      // Measured: Choice keeps options in questionInfo; Ranking and Likert in the choices
      // sub-collection. Read each from where its own type stores them.
      const choices = c.type==='Question.Choice'
        ? (q?.Choices||[]).map(x=>x.Description)
        : [...(c.choices||[])].sort((a,b)=>(a.order??0)-(b.order??0)).map(x=>x.displayText);
      return {id:c.id, title:c.title||'', type:SHORT[c.type]||c.type, order:c.order,
        section_id:section, group_id:c.groupId??null, required:!!c.required,
        number:q?.IsNumber===true, multiline:q?.Multiline===true, options:choices,
        levels:q?.Length??null, shape:q?.RatingShape??null,
        left:q?.LeftDescription??null, right:q?.RightDescription??null,
        max_file_size_mb:q?.MaxFileSize??null, malformed_info:q===null,
        fractional_order: !Number.isFinite(c.order) || c.order % 1000000 !== 0,
        orphan:['Question','Section'].includes((c.title||'').trim())};
    });
  }
  function verify(f, spec) {
    const all = normalize(f), problems = [];
    const add = (i, field, actual, expected) => problems.push({index:i, id:all[i]?.id??null, field, actual, expected});
    for (let i=0;i<Math.max(all.length,spec.length);i++) {
      const c=all[i], s=spec[i];
      if (!c || !s) { add(i, 'card', c||null, s||null); continue; }
      const wanted = s.s ? {type:'Section',title:s.s} : {
        type:s.t,title:s.q,required:!!s.req,number:!!s.num,
        ...(s.t==='Text'?{multiline:!s.num && s.long!==false}:{}),
        ...(s.t==='Choice'||s.t==='Ranking'?{options:s.opts}:{}),
        ...(s.t==='Upload'?{max_file_size_mb:s.mb||10}:{}),
        ...(s.t==='Rating'?{levels:s.levels??5,shape:s.shape??'Star',
          left:s.left??'',right:s.right??''}:{}),
        ...(s.t==='NPS'?{left:s.left??'Not at all likely',
          right:s.right??'Extremely likely'}:{})};
      for (const [key,value] of Object.entries(wanted))
        if (JSON.stringify(c[key])!==JSON.stringify(value)) add(i,key,c[key],value);
      if(c.orphan) add(i,'orphan',true,false);
      if(c.malformed_info) add(i,'malformed_info',true,false);
    }
    return problems;
  }
  window.FORMS = {cards, info, normalize, verify};
})()
