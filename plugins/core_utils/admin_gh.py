from memory import Memory
from cl import DEFAULT_ADMIN

memory = Memory()

def add_admin(sender_id, new_admin):
    """添加管理员（只有主管理员可以操作）"""
    if str(sender_id) != DEFAULT_ADMIN:
        return "权限不足，只有主管理员可以设置管理员"
    if not new_admin.isdigit():
        return "请输入正确的QQ号"
    admins = memory.get("admin_list", [])
    if new_admin not in admins:
        admins.append(new_admin)
        memory.set("admin_list", admins)
        return f"已添加管理员：{new_admin}"
    return f"{new_admin} 已是管理员"

def del_admin(sender_id, del_admin):
    """删除管理员（只有主管理员可以操作）"""
    if str(sender_id) != DEFAULT_ADMIN:
        return "权限不足，只有主管理员可以删除管理员"
    admins = memory.get("admin_list", [])
    if del_admin in admins and del_admin != DEFAULT_ADMIN:
        admins.remove(del_admin)
        memory.set("admin_list", admins)
        return f"已删除管理员：{del_admin}"
    return f"无法删除（不存在或是主管理员）"

def list_admin():
    """列出所有管理员"""
    admins = memory.get("admin_list", [])
    if not admins:
        return "当前没有管理员"
    return "管理员列表：\n" + "\n".join(admins)
