const fs = require('fs');
const https = require('https');
const { execFileSync } = require('child_process');
const path = require('path');

// ============ 站点配置 ============
const CHANNEL = 'vvjxyd';
const API_BASE = 'https://vfg.bogct9.com:52000';   // 域名变了就改这里
const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) ' +
           'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1';
const REFERER = 'https://tob.tobvic.com:52000/'; // 域名变了也改这里

// ============ 从 cookies.json 读 cookie ============
function loadCookieHeader() {
  try {
    const arr = JSON.parse(fs.readFileSync('cookies.json', 'utf8'));
    return arr.map(c => `${c.name}=${c.value}`).join('; ');
  } catch {
    return '';
  }
}

// ============ HTTP GET 返回 JSON ============
function httpGetJson(url, cookie) {
  return new Promise((resolve, reject) => {
    https.get(url, {
      headers: {
        'User-Agent': UA,
        'Referer': REFERER,
        'Cookie': cookie,
        'Accept': 'application/json',
      },
    }, (res) => {
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => {
        const body = Buffer.concat(chunks).toString('utf8');
        try { resolve(JSON.parse(body)); }
        catch (e) { reject(new Error('非 JSON 响应: ' + body.slice(0, 300))); }
      });
    }).on('error', reject);
  });
}

// ============ XOR 解密（和 decrypt_api.js 一致）============
function xorDecrypt(b64Data, keyStr) {
  const buf = Buffer.from(b64Data, 'base64url');
  const key = Buffer.from(keyStr, 'utf8');
  const out = Buffer.alloc(buf.length);
  for (let i = 0; i < buf.length; i++) out[i] = buf[i] ^ key[i % key.length];
  return out.toString('utf8');
}

// ============ 主流程 ============
(async () => {
  const input = process.argv[2];
  if (!input) {
    console.error('用法:');
    console.error('  node fetch_by_id.js <视频页 URL>');
    console.error('  node fetch_by_id.js <video id>');
    console.error('例:');
    console.error('  node fetch_by_id.js "https://xxx/video/detail/91892"');
    console.error('  node fetch_by_id.js 91892');
    process.exit(1);
  }

  // ---- 从输入里抠 video id ----
  let videoId;
  const m = input.match(/\/video\/detail\/(\d+)/);
  if (m) videoId = m[1];
  else if (/^\d+$/.test(input)) videoId = input;
  else { console.error('无法提取 video id:', input); process.exit(1); }
  console.log('video id =', videoId);

  // ---- 准备 cookie ----
  const cookie = loadCookieHeader();
  console.log('cookie 长度 =', cookie.length);
  if (!cookie) {
    console.warn('⚠ 没有 cookie，很可能被 JS 挑战拦截。请先跑一次 diag.js');
  }

  // ---- 调详情 API ----
  const apiUrl = `${API_BASE}/api/v3/home/public/video/long/detail?channel=${CHANNEL}&id=${videoId}`;
  console.log('请求:', apiUrl);

  let resp;
  try {
    resp = await httpGetJson(apiUrl, cookie);
  } catch (e) {
    console.error('请求失败:', e.message);
    console.error('提示: 检查 API_BASE 域名是否还有效（重跑 diag.js 看日志）');
    process.exit(1);
  }

  console.log('响应 code =', resp.code, 'message =', resp.message);

  if (resp.code !== 200) {
    console.error('接口非 200，多半是 cookie 过期。请跑: node diag.js');
    process.exit(1);
  }

  // ---- XOR 解密 ----
  const plain = xorDecrypt(resp.data, resp.key);
  const detail = JSON.parse(plain);

  console.log('\n=== 视频信息 ===');
  console.log('标题   :', detail.title);
  console.log('作者   :', detail.author);
  console.log('分类   :', detail.classify);
  console.log('时长   :', detail.duration || '(接口未返回)');
  console.log('m3u8   :', detail.play_hls_url);

  fs.writeFileSync(`detail_${videoId}.json`, plain);
  console.log(`\n已保存 detail_${videoId}.json`);

  // ---- 交给 download_video.js ----
  if (!detail.play_hls_url) {
    console.error('play_hls_url 为空，可能视频下架 / 需要会员 / 需要 App');
    process.exit(1);
  }

  console.log('\n=== 开始下载视频 ===\n');
  execFileSync('node', [path.join(__dirname, 'download_video.js'), detail.play_hls_url], { stdio: 'inherit' });
})();
