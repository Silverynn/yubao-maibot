// 优先使用双方实际交付的模型；伙伴没有这些资源时仍可选择本机已有模型。
export function defaultAvatars(catalog){
  const names=catalog.map(model=>model.name);
  const own=names.includes('moonbell-20261003')?'moonbell-20261003':names[0]||'none';
  const partner=names.includes('yubao-fullbody')?'yubao-fullbody':names.find(name=>name!==own)||own;
  return [own,partner];
}
