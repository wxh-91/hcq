const { chromium } = require('playwright');
const fs = require('fs');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    args: [
      '--disable-web-security',
      '--disable-features=IsolateOrigins,site-per-process,OutOfBlinkCors',
      '--no-sandbox',
      '--disable-blink-features=AutomationControlled',
    ],
  });

  const context = await browser.newContext({
    userAgent:
      'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) ' +
      'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
    locale: 'zh-CN',
  });

await context.addInitScript(() => {
  const log = (tag, ...args) => console.log('[HOOK-' + tag + ']', ...args);

  // === 1. hook CryptoJS 各类 decrypt ===
  let hooked = false;
  const tryHookCryptoJS = () => {
    if (!window.CryptoJS || hooked) return false;
    const C = window.CryptoJS;
    ['AES', 'RC4', 'Rabbit', 'TripleDES', 'DES'].forEach(algo => {
      if (C[algo] && C[algo].decrypt) {
        const orig = C[algo].decrypt;
        C[algo].decrypt = function(cipher, key, cfg) {
          try {
            log(algo + '.decrypt',
              'key=', key && key.toString ? key.toString() : String(key),
              'iv=', cfg && cfg.iv && cfg.iv.toString ? cfg.iv.toString() : null,
              'mode=', cfg && cfg.mode ? cfg.mode.toString() : null,
              'padding=', cfg && cfg.padding ? cfg.padding.toString() : null,
              'cipherHead=', String(cipher).slice(0, 80)
            );
          } catch(_) {}
          return orig.apply(this, arguments);
        };
        log('hook', algo + '.decrypt installed');
      }
    });
    hooked = true;
    return true;
  };
  if (!tryHookCryptoJS()) {
    let n = 0;
    const t = setInterval(() => { if (tryHookCryptoJS() || ++n > 200) clearInterval(t); }, 50);
  }

  // === 2. hook WebCrypto (subtle.decrypt) ===
  if (window.crypto && window.crypto.subtle) {
    const origDecrypt = window.crypto.subtle.decrypt.bind(window.crypto.subtle);
    window.crypto.subtle.decrypt = async function(alg, key, data) {
      try {
        let keyHex = null;
        try {
          const raw = await window.crypto.subtle.exportKey('raw', key);
          keyHex = Array.from(new Uint8Array(raw)).map(b => b.toString(16).padStart(2,'0')).join('');
        } catch(_) {}
        log('subtle.decrypt',
          'alg=', JSON.stringify(alg),
          'iv=', alg && alg.iv ? Array.from(new Uint8Array(alg.iv)).map(b=>b.toString(16).padStart(2,'0')).join('') : null,
          'key=', keyHex,
          'dataLen=', data && data.byteLength
        );
      } catch(_) {}
      return origDecrypt(alg, key, data);
    };
    log('hook', 'subtle.decrypt installed');
  }

  // === 3. hook atob，看谁在做 base64 解码 ===
  const origAtob = window.atob;
  window.atob = function(s) {
    if (typeof s === 'string' && s.length > 200) {
      log('atob', 'len=', s.length, 'head=', s.slice(0, 60));
      // 打印调用栈
      try { log('atob-stack', new Error().stack.split('\n').slice(1, 6).join('\n')); } catch(_) {}
    }
    return origAtob.call(this, s);
  };

  // === 4. hook String.fromCharCode / XOR 风格解密 ===
  const origFromCharCode = String.fromCharCode;
  String.fromCharCode = function(...args) {
    if (args.length > 100) {
      log('fromCharCode', 'argsLen=', args.length, 'first10=', args.slice(0, 10));
    }
    return origFromCharCode.apply(this, args);
  };
});

  const page = await context.newPage();
  fs.writeFileSync('api.json', '');

  // 只打印关键 API，其它全部忽略
  const KEYS = [
    '/video/long/detail',
    '/video/getRelateList',
    '/domain/jump',
    'init2',
    '/api-user/v3/member/detail',
  ];

  page.on('response', async (res) => {
    const url = res.url();
    if (!KEYS.some(k => url.includes(k))) return;
    console.log('\n=== API ===', res.status(), url);
    try {
      const body = await res.text();
      console.log(body.slice(0, 5000));
      fs.appendFileSync('api.json', url + '\n' + body + '\n\n');
    } catch (e) {
      console.log('[err]', e.message);
    }
  });

  // 请求 cookie 变化也记一下，方便后面 curl
  page.on('response', async (res) => {
    const url = res.url();
    if (url.includes('__js_challenge') || url.includes('/video/detail/')) {
      const setCookie = res.headers()['set-cookie'];
      if (setCookie) console.log('[SET-COOKIE]', setCookie);
    }
  });

  page.on('pageerror', (err) => console.log('[PAGEERROR]', err.message));

  const targetUrl = 'https://tob.tobvic.com:52000/video/detail/91891';
  console.log('访问:', targetUrl);
  await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });

  // 等 90 秒，别中断！
  console.log('等待 90 秒，请勿中断...');
  await page.waitForTimeout(90000);

  // 拿 cookie
  const cookies = await context.cookies();
  fs.writeFileSync('cookies.json', JSON.stringify(cookies, null, 2));
  console.log('Cookie 已存到 cookies.json');
  console.log(cookies.map(c => c.name + '=' + c.value).join('; '));

  // 打印 video 标签
  const videos = await page.$$eval('video', els =>
    els.map(v => ({ src: v.src, currentSrc: v.currentSrc, readyState: v.readyState }))
  );
  console.log('video:', JSON.stringify(videos, null, 2));

  await page.screenshot({ path: 'page.png' });
  await browser.close();
})();
