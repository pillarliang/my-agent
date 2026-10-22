
(() => {
 const navLinks=[...document.querySelectorAll('[data-chapter]')];
 const search=document.getElementById('chapter-search');
 const count=document.getElementById('search-count');
 const corpus=new Map(navLinks.map(a=>[a.dataset.chapter,a.textContent.toLowerCase()]));
 search.addEventListener('input',()=>{
  const term=search.value.trim().toLowerCase();let found=0;
  for(const a of navLinks){a.hidden=Boolean(term&&!corpus.get(a.dataset.chapter).includes(term));if(!a.hidden)found++;}
  count.textContent=term?`${found} 个相关章节`:'';
 });
 const here=location.pathname;
 for(const a of navLinks){
  const target=new URL(a.href,location.href);
  if(target.pathname===here && document.getElementById(a.dataset.chapter)){
   a.classList.add('active');a.setAttribute('aria-current','page');
  }
  a.addEventListener('click',()=>{if(matchMedia('(max-width:720px)').matches)document.getElementById('chapter-menu').open=false;});
 }
 const dialog=document.getElementById('diagram-view');
 const canvas=document.getElementById('diagram-canvas');
 const closeDialog=()=>{dialog.close();canvas.replaceChildren();};
 document.getElementById('diagram-close').addEventListener('click',closeDialog);
 document.getElementById('diagram-size').addEventListener('click',e=>{const actual=canvas.classList.toggle('actual');e.target.textContent=actual?'适应窗口':'原始尺寸';});
 dialog.addEventListener('click',e=>{if(e.target===dialog)closeDialog();});
 for(const figure of document.querySelectorAll('figure.am-diagram')){
  const svg=figure.querySelector('svg, img');if(!svg)continue;
  const actions=document.createElement('div');actions.className='diagram-actions';
  const button=document.createElement('button');button.type='button';button.textContent='放大查看';
  button.addEventListener('click',()=>{canvas.classList.remove('actual');document.getElementById('diagram-size').textContent='原始尺寸';canvas.replaceChildren(svg.cloneNode(true));document.getElementById('diagram-title').textContent=svg.getAttribute('aria-label')||svg.getAttribute('alt')||'图示';dialog.showModal();});
  actions.append(button);figure.prepend(actions);
 }
 for(const code of document.querySelectorAll('pre code')){
  const pre=code.closest('pre');
  const block=pre.closest('.am-codeblock')||pre.parentElement;
  const header=block.querySelector('.am-code-head');
  const button=document.createElement('button');button.className='code-copy';button.type='button';button.textContent='复制代码';
  button.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(code.textContent);button.textContent='已复制';setTimeout(()=>button.textContent='复制代码',1400);}catch{const range=document.createRange();range.selectNodeContents(code);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);button.textContent='已选中，请复制';}});
  (header||block).append(button);
 }
})();
