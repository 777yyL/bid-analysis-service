export interface CategoryNode {value:string;label:string;children?:CategoryNode[]}
export function categoryParts(value:string):string[]{return value.split(/\s*(?:\/|／|>|＞|›|→)\s*/).map(v=>v.trim()).filter(Boolean);}
export function categoryTree(values:string[],items:{id:string;name:string;categories:string[]}[]=[]):CategoryNode[]{
 const roots:CategoryNode[]=[];
 for(const value of values){let nodes=roots;const parts=categoryParts(value);parts.forEach((label,index)=>{
  const id=parts.slice(0,index+1).join('/');let node=nodes.find(n=>n.value===id);
  if(!node){node={value:id,label};nodes.push(node);}
  if(index<parts.length-1){node.children??=[];nodes=node.children;}
 });}
 for(const item of items)for(const category of item.categories){
  const parts=categoryParts(category);let nodes=roots;let parent:CategoryNode|undefined;
  for(let i=0;i<parts.length;i++){parent=nodes.find(n=>n.value===parts.slice(0,i+1).join('/'));if(!parent)break;nodes=parent.children||[];}
  if(parent){parent.children??=[];const value='@sales:'+item.id;if(!parent.children.some(n=>n.value===value))parent.children.push({value,label:item.name});}
 }
 return roots;
}
