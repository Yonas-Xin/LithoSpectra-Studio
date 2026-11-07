# -*- coding: utf-8 -*-
"""样本树右键与「编辑」菜单里整理类别的那些操作。

分工与 :mod:`.app` 开头说的一致：复制、粘贴、改名、恢复、新建工作区直接落库
（会话撤销栈）；合并、删工区动到样本归属，走暂存区，保存前都能撤销。
"""

from typing import List, Optional

from pydantic import BaseModel


class CopyBody(BaseModel):
    ids: List[int]
    parent_id: Optional[int] = None      # 粘到分组下
    region: Optional[str] = None         # 粘到工区顶层
    target_id: Optional[int] = None      # 粘到基础类别：复制样本进去


class MergeBody(BaseModel):
    ids: List[int]
    target_id: int


class RegionBody(BaseModel):
    region: str
    name: str = ""                       # 新名字 / 新工作区下第一个类别的名字


def register(app, st, guard) -> None:
    s = st.session

    def real(ids) -> List[int]:
        out = [int(i) for i in ids]
        if any(i < 0 for i in out):
            raise ValueError("新建的类别尚未保存，请保存后再进行此操作。")
        missing = [i for i in out if i not in s.class_map]
        if missing:
            raise KeyError("类别不存在：%s" % missing[0])
        return out

    @app.post("/api/classes/paste")
    def paste(body: CopyBody):
        """把剪贴板里的类别粘到目标上：工区 / 分组复制整支，基础类别复制样本进去。"""
        def run():
            ids = real(body.ids)
            if body.target_id is not None:
                target = s.class_map.get(int(body.target_id))
                if target is None:
                    raise KeyError("目标类别不存在。")
                if target.is_group:
                    made = s.copy_classes(ids, parent_id=target.class_id)
                    msg = "复制进分组「%s」，得到 %d 个新类别" % (target.name, len(made))
                else:
                    n = s.copy_samples_into(ids, target.class_id)
                    msg = "把 %d 条样本复制进「%s」" % (n, target.name)
            else:
                region = body.region or ""
                made = s.copy_classes(ids, region=region)
                msg = "复制到工区「%s」顶层，得到 %d 个新类别" % (region or "未分组", len(made))
            st.lib_changed()
            return {"message": msg}
        return guard(run)

    @app.post("/api/classes/merge")
    def merge(body: MergeBody):
        """把勾选的类别（整支）并进目标，然后删掉它们。目标的上级不算源。"""
        def run():
            buf = st.require_buffer()
            target = body.target_id
            ancestors = set(s.ancestor_ids(target)) if target in s.class_map else set()
            sources = [c for c in body.ids if c != target and c not in ancestors]
            if not sources:
                raise ValueError("勾选的类别均为目标类别的上级，无法合并。")
            moved = buf.merge_classes(sources, target)
            st.buf_changed()
            name = buf.class_map().get(target)
            return {"message": "已将 %d 个要素合并到「%s」，%d 个类别待删除。保存前可撤销。"
                    % (moved, name.name if name else target, len(sources))}
        return guard(run)

    @app.post("/api/classes/{class_id}/restore")
    def restore_class(class_id: int):
        def run():
            real([class_id])
            ok = s.restore_class(class_id)
            if ok:
                st.lib_changed()
            return {"ok": ok, "message": "已恢复到导入时的状态" if ok
                    else "这个类别是后来新建的，没有可恢复的原始状态。"}
        return guard(run)

    @app.get("/api/classes/edited")
    def edited():
        def run():
            return [{"id": c.class_id, "path": s.class_path(c.class_id),
                     "origin": c.origin.get("name") or "-"} for c in s.edited_classes()]
        return guard(run)

    @app.post("/api/classes/restore-all")
    def restore_all():
        """整库恢复到导入时的分类状态。会清空撤销栈，界面在调之前已经确认过。"""
        def run():
            stats = s.restore_all()
            st.history = []
            st.export_merge.clear()
            st.export_split.clear()
            st.require_buffer().rebase()
            return stats
        return guard(run)

    @app.post("/api/regions/rename")
    def rename_region(body: RegionBody):
        def run():
            name = body.name.strip()
            if not name:
                raise ValueError("工区名称不能为空。")
            n = s.rename_region(body.region, name)
            st.lib_changed()
            return {"message": "已重命名工区，影响 %d 个类别" % n}
        return guard(run)

    @app.post("/api/regions/delete")
    def delete_region(body: RegionBody):
        def run():
            n_cls, n = st.require_buffer().delete_region(body.region)
            st.buf_changed()
            return {"message": "已删除工区「%s」：%d 个类别，%d 个要素（未保存）"
                    % (body.region or "未分组", n_cls, n)}
        return guard(run)

    @app.post("/api/regions/new")
    def new_region(body: RegionBody):
        """新建工作区。工作区只是类别上的一个字段，空工作区留不住，所以连第一个类别一起建。"""
        def run():
            region, first = body.region.strip(), body.name.strip()
            if not region or not first:
                raise ValueError("请填写工区名称与第一个类别名称。")
            if region in s.regions():
                raise ValueError("已存在同名工区「%s」。" % region)
            cid = s.add_class(first, region=region)
            st.lib_changed()
            return {"id": cid, "message": "已新建工区「%s」及其类别「%s」。" % (region, first)}
        return guard(run)

    @app.get("/api/regions/{region}/count")
    def region_count(region: str):
        def run():
            ids = s.region_class_ids(region)
            return {"classes": len(ids), "samples": len(s.keys_of_classes(ids))}
        return guard(run)

