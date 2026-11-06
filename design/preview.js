// 仅供本地设计预览: 文件改变后刷新, 导出时隐藏预览控件.
if(!new URLSearchParams(location.search).has('capture')){
 const tools=document.createElement('div');tools.dataset.preview='true';
 tools.style.cssText='position:fixed;bottom:15px;right:18px;z-index:9999;display:flex;gap:13px;align-items:center;background:#fffef7ed;border:1px solid #d7d9ce;padding:9px 14px;font:11px sans-serif;color:#69745e;box-shadow:0 2px 9px #2020200b';
 tools.innerHTML='<span style="color:#6a825f">●</span><span>实时预览</span><a href="/" style="border-left:1px solid #ddd;padding-left:13px">全部21个页面 ↗</a>';document.body.append(tools);
 let signature;setInterval(async()=>{try{const data=await Promise.all(['ui.html','ui.css','redesign.js','screens.js'].map(async f=>{const r=await fetch(f,{cache:'no-store'});return await r.text()}));const next=data.join('');if(signature&&signature!==next)location.reload();signature=next}catch{}},1800);
}
