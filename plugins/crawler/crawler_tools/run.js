const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const code = process.argv[2];
if (!code) {
    console.log(JSON.stringify({ error: "缺少 code 参数" }));
    process.exit(1);
}

const workspace = path.join(__dirname, 'workspace');
if (!fs.existsSync(workspace)) fs.mkdirSync(workspace, { recursive: true });

const tempFile = path.join(workspace, `temp_run_${Date.now()}.js`);
fs.writeFileSync(tempFile, code, 'utf-8');

try {
    // 执行 node，超时限制 10 秒
    const output = execSync(`node ${tempFile}`, { timeout: 10000, encoding: 'utf-8' });
    console.log(JSON.stringify({ success: true, output: output.trim() }));
} catch (e) {
    console.log(JSON.stringify({ 
        success: false, 
        error: e.message,
        stdout: e.stdout ? e.stdout.toString() : "",
        stderr: e.stderr ? e.stderr.toString() : ""
    }));
} finally {
    // 清理临时文件
    if (fs.existsSync(tempFile)) fs.unlinkSync(tempFile);
}
