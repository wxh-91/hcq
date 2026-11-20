import sys
import os
import jmcomic
from pypdf import PdfMerger  # 依赖之前装好的 pypdf

def merge_album_pdfs(folder_path):
    """把目录下的所有 pdf 按顺序合并成一个全本 pdf"""
    if not os.path.exists(folder_path):
        print(f"[-] 找不到下载目录: {folder_path}")
        return

    # 找到目录下所有的 pdf 文件
    pdf_files = [f for f in os.listdir(folder_path) if f.lower().endswith('.pdf')]
    if len(pdf_files) <= 1:
        print("[*] 只有 1 个或 0 个 PDF，无需合并。")
        return

    # 按文件名排序（通常是 1.pdf, 2.pdf...）
    pdf_files.sort()
    
    merger = PdfMerger()
    try:
        print(f"[*] 发现 {len(pdf_files)} 个分卷，开始合并...")
        for pdf in pdf_files:
            merger.append(os.path.join(folder_path, pdf))
        
        # 生成最终合并的文件名
        album_name = os.path.basename(os.path.normpath(folder_path))
        output_path = os.path.join(os.path.dirname(folder_path), f"{album_name}_全本.pdf")
        
        merger.write(output_path)
        merger.close()
        print(f"[+] 合并成功！全本文件：{output_path}")
        
        # 清理掉原来的分卷文件
        for pdf in pdf_files:
            os.remove(os.path.join(folder_path, pdf))
        os.rmdir(folder_path) # 删掉空文件夹
        print("[*] 已清理原来的碎文件。")
        
    except Exception as e:
        print(f"[-] 合并 PDF 失败: {e}")
        merger.close()

def main():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    option_path = os.path.join(current_dir, 'option.yml')

    if not os.path.exists(option_path):
        print(f"[-] 错误: 找不到配置文件 {option_path}")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("[*] 用法: python jm.py <Album_ID>")
        sys.exit(1)

    album_id = sys.argv[1]
    print(f"[*] 初始化配置: {option_path}")
    
    try:
        option = jmcomic.create_option_by_file(option_path)
        
        # 获取配置的下载根目录，方便后续去那儿找合并结果
        base_dir = option.dir_rule.base_dir
        
        print(f"[*] 开始下载 Album ID: {album_id} ...")
        jmcomic.download_album(album_id, option)
        print("[+] 下载阶段完成！")
        
        # 极其暴力的寻找刚下好的目录
        # 因为库默认按 Bd_Aauthor_Atitle 建目录，我们直接在 base_dir 下找最新修改的文件夹
        if os.path.exists(base_dir):
            subdirs = [os.path.join(base_dir, d) for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))]
            if subdirs:
                # 找到最新修改的那个作者目录，再找里面的本子目录
                latest_author_dir = max(subdirs, key=os.path.getmtime)
                album_dirs = [os.path.join(latest_author_dir, d) for d in os.listdir(latest_author_dir) if os.path.isdir(os.path.join(latest_author_dir, d))]
                if album_dirs:
                    latest_album_dir = max(album_dirs, key=os.path.getmtime)
                    # 执行合并
                    merge_album_pdfs(latest_album_dir)
        
    except Exception as e:
        print(f"[-] 运行过程中发生异常: {e}")

if __name__ == "__main__":
    main()
