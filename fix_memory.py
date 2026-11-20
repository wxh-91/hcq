import json, shutil

p = '/home/wxh/robot/memory.json'
shutil.copy(p, p + '.bak')

lines = open(p, encoding='utf-8').read().split('\n')

# 1-indexed: 29 行是目标内容
idx = None
for i, l in enumerate(lines):
    if '首归之女' in l:
        idx = i
        break

if idx is None:
    print('NOT_FOUND')
    raise SystemExit

# 目标行是对象内的 "content" 字段，向上找 '{' 向下找 '}'
start = idx
while lines[start].strip() != '{':
    start -= 1
end = idx
while lines[end].strip() != '}':
    end += 1

# 处理逗号：若对象后面还有元素，保留尾部逗号
del lines[start:end + 1]

open(p, 'w', encoding='utf-8').write('\n'.join(lines))

d = json.load(open(p, encoding='utf-8'))
print('JSON_OK')
print('memories =', d.get('memories'))
print('has_target =', '首归之女' in open(p, encoding='utf-8').read())
print('diff_other_keys =', len(d))
