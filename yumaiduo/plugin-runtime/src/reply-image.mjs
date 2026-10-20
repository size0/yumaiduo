import {readOfficialShops} from './shops.mjs';

export function validateReplyImage(image) {
  const url=new URL(image?.imageUrl);
  if(url.protocol!=='https:' || url.username || url.password ||
      !(url.hostname==='alicdn.com'||url.hostname.endsWith('.alicdn.com')) ||
      !['width','height'].every(k=>Number.isSafeInteger(image[k])&&image[k]>0&&image[k]<=20000))
    throw new Error('图片地址或尺寸无效');
  return {imageUrl:image.imageUrl,width:image.width,height:image.height};
}

export async function uploadReplyImage(body,client,tenant) {
  if(!body || typeof body.data!=='string' || body.data.length>7*1024*1024 ||
      !/^[A-Za-z0-9+/]+={0,2}$/.test(body.data)) throw new Error('图片内容无效');
  const data=Buffer.from(body.data,'base64');
  if(!data.length || data.length>5*1024*1024) throw new Error('图片不能超过5MB');
  const png=data.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]));
  const jpg=data[0]===255&&data[1]===216&&data[2]===255;
  const webp=data.toString('ascii',0,4)==='RIFF'&&data.toString('ascii',8,12)==='WEBP';
  const contentType=png?'image/png':jpg?'image/jpeg':webp?'image/webp':null;
  if(!contentType) throw new Error('请选择PNG、JPG或WEBP图片');
  const shops=await readOfficialShops(client,tenant,true);
  if(!shops.some(s=>s.shop_id===body.accountUnb&&s.platform_enabled)) throw new Error('请选择已授权店铺');
  return validateReplyImage(await client.im.uploadImage({accountUnb:body.accountUnb,
    filename:png?'reply.png':jpg?'reply.jpg':'reply.webp',contentType,data}));
}

export async function sendReplyMessage(command,client) {
  const address={accountUnb:command.shop_id,chatId:command.chat_id,peerUnb:command.buyer_id};
  return command.image ? client.im.sendImage({...address,...validateReplyImage(command.image)})
    : client.im.sendMessage({...address,text:command.text});
}
