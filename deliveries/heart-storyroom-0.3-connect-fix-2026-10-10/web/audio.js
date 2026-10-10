// 转换浏览器PCM为本地识别服务要求的16kHz单声道WAV，不保存录音文件。
export function wavBase64(chunks, rate) {
  const count=chunks.reduce((n,c)=>n+c.length,0), input=new Float32Array(count);
  let offset=0;for(const chunk of chunks){input.set(chunk,offset);offset+=chunk.length;}
  const length=Math.floor(count*16000/rate), buffer=new ArrayBuffer(44+length*2), view=new DataView(buffer);
  const str=(at,s)=>{for(let i=0;i<s.length;i++)view.setUint8(at+i,s.charCodeAt(i));};
  str(0,'RIFF');view.setUint32(4,36+length*2,true);str(8,'WAVE');str(12,'fmt ');view.setUint32(16,16,true);
  view.setUint16(20,1,true);view.setUint16(22,1,true);view.setUint32(24,16000,true);view.setUint32(28,32000,true);
  view.setUint16(32,2,true);view.setUint16(34,16,true);str(36,'data');view.setUint32(40,length*2,true);
  for(let i=0;i<length;i++){
    const start=Math.floor(i*rate/16000),end=Math.max(start+1,Math.floor((i+1)*rate/16000));
    let sum=0;for(let j=start;j<Math.min(end,count);j++)sum+=input[j];
    const sample=Math.max(-1,Math.min(1,sum/(Math.min(end,count)-start)));
    view.setInt16(44+i*2,sample<0?sample*32768:sample*32767,true);
  }
  let binary='';for(const byte of new Uint8Array(buffer))binary+=String.fromCharCode(byte);
  return btoa(binary);
}

export function fastSignal(text) {
  return /^(停一下|别说了|先停|暂停|停止|等一下)[，。！？,.!?\s]*$/.test(text.trim());
}
