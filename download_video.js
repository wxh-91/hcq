const crypto = require('crypto');
const fs = require('fs');
const https = require('https');
const http = require('http');
const path = require('path');

const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1';
const REFERER = 'https://tob.tobvic.com:52000/';

function httpGet(url, headers = {}) {
  return new Promise((resolve, reject) => {
    const mod = url.startsWith('https') ? https : http;
    mod.get(url, { headers: { 'User-Agent': UA, 'Referer': REFERER, ...headers } }, (res) => {
      if ([301,302,303,307,308].includes(res.statusCode) && res.headers.location) {
        return httpGet(res.headers.location, headers).then(resolve, reject);
      }
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
    }).on('error', reject);
  });
}

(async () => {
  const m3u8Url = process.argv[2];
  if (!m3u8Url) { console.error('用法: node download_video.js "<m3u8 url>"'); process.exit(1); }

  console.log('拉取 m3u8:', m3u8Url);
  const res = await httpGet(m3u8Url);
  const m3u8Text = res.body.toString('utf8');
  fs.writeFileSync('playlist.m3u8', m3u8Text);

  // 解析 EXT-X-KEY 和分片
  const lines = m3u8Text.split(/\r?\n/);
  const baseUrl = new URL(m3u8Url);

  let keyUri = null, keyIvHex = null;
  const segments = [];
  for (const ln of lines) {
    if (ln.startsWith('#EXT-X-KEY')) {
      const m1 = ln.match(/URI="([^"]+)"/);
      const m2 = ln.match(/IV=0x([0-9a-fA-F]+)/);
      if (m1) keyUri = m1[1];
      if (m2) keyIvHex = m2[1];
    }
    if (ln && !ln.startsWith('#')) {
      segments.push(new URL(ln, baseUrl).href);
    }
  }

  // 解析 key URL（可能是相对路径）
  let keyUrl;
  if (keyUri.startsWith('http')) keyUrl = keyUri;
  else keyUrl = new URL(keyUri, baseUrl.origin).href;

  console.log('\nKEY URI:', keyUrl);
  console.log('IV (hex):', keyIvHex);
  console.log('分片数量:', segments.length);

  // 下载 key
  const keyRes = await httpGet(keyUrl);
  let keyBuf = keyRes.body;
  console.log('key 原始长度:', keyBuf.length);

  // key 可能是 16 字节二进制，也可能是 32 字节 hex 字符串
  if (keyBuf.length === 32 && /^[0-9a-fA-F]{32}$/.test(keyBuf.toString('utf8').trim())) {
    keyBuf = Buffer.from(keyBuf.toString('utf8').trim(), 'hex');
    console.log('key 是 hex 字符串，解码后长度:', keyBuf.length);
  }

  if (keyBuf.length !== 16) {
    console.log('⚠ key 长度不是 16，前 32 字节:', keyBuf.slice(0, 32).toString('hex'));
    // 如果返回的是 JSON，也许里面有 key
    try {
      const j = JSON.parse(keyBuf.toString('utf8'));
      console.log('key 响应是 JSON:', j);
      if (j.key) keyBuf = Buffer.from(j.key, 'hex');
    } catch (_) {}
  }

  console.log('最终 key hex:', keyBuf.toString('hex'));
  const iv = Buffer.from(keyIvHex, 'hex');
  console.log('IV hex:', iv.toString('hex'), '长度:', iv.length);

  // 下载并解密
  const outDir = './segments';
  if (!fs.existsSync(outDir)) fs.mkdirSync(outDir);
  const decrypted = [];

  for (let i = 0; i < segments.length; i++) {
    const u = segments[i];
    process.stdout.write(`[${i+1}/${segments.length}] `);
    try {
      const r = await httpGet(u);
      const ct = r.body;
      // HLS AES-128-CBC 用 m3u8 里的 IV，整段解密，padding=PKCS7
      const d = crypto.createDecipheriv('aes-128-cbc', keyBuf, iv);
      d.setAutoPadding(true);
      const plain = Buffer.concat([d.update(ct), d.final()]);
      const fn = path.join(outDir, String(i).padStart(5, '0') + '.ts');
      fs.writeFileSync(fn, plain);
      decrypted.push(fn);
      const tsMark = plain[0] === 0x47 ? '✓TS' : '?' ;
      console.log(`${tsMark} ${plain.length}B`);
    } catch (e) {
      console.log('✗', e.message);
      // 试 no padding
      try {
        const r = await httpGet(u);
        const d = crypto.createDecipheriv('aes-128-cbc', keyBuf, iv);
        d.setAutoPadding(false);
        const plain = Buffer.concat([d.update(r.body), d.final()]);
        const fn = path.join(outDir, String(i).padStart(5, '0') + '.ts');
        fs.writeFileSync(fn, plain);
        decrypted.push(fn);
        console.log(`  noPad ✓ ${plain.length}B head=${plain[0].toString(16)}`);
      } catch (e2) {
        console.log('  noPad 也失败:', e2.message);
      }
    }
  }

  const merged = decrypted.map(f => fs.readFileSync(f));
  fs.writeFileSync('video.ts', Buffer.concat(merged));
  console.log('\n合并完成: video.ts (' + fs.statSync('video.ts').size + ' 字节)');

  const buf = fs.readFileSync('video.ts');
  const isTs = buf[0] === 0x47 && buf[188] === 0x47 && buf[376] === 0x47;
  console.log('TS sync check:', isTs ? '✓ PASS' : '✗ FAIL');
  if (!isTs) {
    console.log('前 32 字节 hex:', buf.slice(0, 32).toString('hex'));
  }
})();
