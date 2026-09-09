// Development-only optimizer. Python installations use committed assets.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const root=path.resolve(__dirname,'..');
const tools=require('node:module').createRequire(path.resolve(root,process.argv[2]||'scripts/asset-tools','package.json'));
const CleanCSS=tools('clean-css'),terser=tools('terser'),acorn=tools('acorn');
const sources=Object.fromEntries(['index.html','spend.css','spend.js','request-state.js','product.js','connections.js','plans-value.js'].map(name=>[name,fs.readFileSync(path.join(root,'frontend_src',name),'utf8').replace(/\r\n/g,'\n')]));
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
function walk(node,visit){if(!node||typeof node!=='object')return;visit(node);for(const value of Object.values(node)){if(Array.isArray(value))value.forEach(child=>walk(child,visit));else if(value&&typeof value==='object')walk(value,visit);}}
// Alias only immutable literal classes. Keep original classes in the DOM;
// classList/className state changes and every public ID remain unchanged.
const dynamic=new Set();
for(const [name,code] of Object.entries(sources).filter(([name])=>name.endsWith('.js'))){
  walk(acorn.parse(code,{ecmaVersion:'latest'}),node=>{
    if(node.type==='Literal'&&typeof node.value==='string'&&/^[\w -]+$/.test(node.value))node.value.split(/\s+/).forEach(value=>dynamic.add(value));
    const listCall=node.type==='CallExpression'&&node.callee?.object?.property?.name==='classList';
    const className=node.type==='AssignmentExpression'&&node.left?.property?.name==='className';
    if(listCall||className)for(const token of code.slice(node.start,node.end).matchAll(/["'`]([\w -]+)["'`]/g))token[1].split(/\s+/).forEach(value=>dynamic.add(value));
  });
}
const content=Object.entries(sources).filter(([name])=>name!=='spend.css').map(([,code])=>code).join('\n');
const staticNames=new Set();
for(const attribute of content.matchAll(/class=(["'])(.*?)\1/gs))attribute[2].split(/\s+/).filter(value=>/^[\w-]+$/.test(value)).forEach(value=>staticNames.add(value));
const aliases=new Map();
const originalClasses=new Set([...sources['spend.css'].matchAll(/\.([A-Za-z_][\w-]*)/g)].map(match=>match[1]));
let aliasSequence=0;
function nextAlias(){let result;do{let n=aliasSequence++;result='';do{result='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'[n%52]+result;n=Math.floor(n/52)-1;}while(n>=0);}while(originalClasses.has(result)||staticNames.has(result)||dynamic.has(result));return result;}
for(const match of sources['spend.css'].matchAll(/\.([A-Za-z_][\w-]*)/g)){
  const name=match[1];if(name.length>4&&staticNames.has(name)&&!dynamic.has(name)&&!aliases.has(name))aliases.set(name,nextAlias());
}
function aliasAttributes(code){return code.replace(/class=(["'])(.*?)\1/gs,(whole,quote,value)=>{const split=value.indexOf('${'),prefix=split<0?value:value.slice(0,split),suffix=split<0?'':value.slice(split);return 'class='+quote+prefix.replace(/[\w-]+/g,name=>aliases.has(name)?name+' '+aliases.get(name):name)+suffix+quote;});}
async function main(){
  const outputs={};
  const css=sources['spend.css'].replace(/\.([A-Za-z_][\w-]*)/g,(whole,name)=>aliases.has(name)?'.'+aliases.get(name):whole);
  const optimized=new CleanCSS({level:{2:{restructureRules:true}},format:false}).minify(css);
  if(optimized.errors.length)throw new Error(optimized.errors.join('\n'));
  outputs['spend.css']=optimized.styles+'\n';
  // One optional-dialog bundle shares compression across the plan, value and
  // connection views. Keep the ordered product entry point separate from it.
  outputs['dialogs.js']='window.initializeProductDialogs=()=>{'+['connections.js','plans-value.js','product.js'].map(name=>aliasAttributes(sources[name])).join('\n;\n')+'\n};';
  outputs['product.js']='window.initializeProductDialogs();';
  for(const name of ['spend.js','request-state.js'])outputs[name]=aliasAttributes(sources[name]);
  for(const name of Object.keys(outputs).filter(name=>name.endsWith('.js'))){const result=await terser.minify(outputs[name],{compress:{passes:3},mangle:true,keep_fnames:name==='spend.js',format:{comments:false}});outputs[name]=result.code+'\n';}
  outputs['index.html']=aliasAttributes(sources['index.html']).replace('    <script src="/connections.js?v=1" defer></script>','    <script src="/dialogs.js?v=1" defer></script>');
  for(const name of ['connections.js','product-helpers.js','harness.js'])fs.rmSync(path.join(root,'spend_web',name),{force:true});
  for(const [name,code] of Object.entries(outputs))fs.writeFileSync(path.join(root,'spend_web',name),code);
  fs.writeFileSync(path.join(root,'spend_web','assets.json'),JSON.stringify({sources:Object.fromEntries(Object.entries(sources).map(([name,code])=>[name,hash(code)])),outputs:Object.fromEntries(Object.entries(outputs).map(([name,code])=>[name,hash(code)])),aliases:Object.fromEntries(aliases)},null,2)+'\n');
  console.log('Optimized browser assets; original DOM classes and IDs preserved.');
}
module.exports={aliases,aliasAttributes,dynamic};
if(require.main===module)main().catch(error=>{console.error(error);process.exitCode=1;});
