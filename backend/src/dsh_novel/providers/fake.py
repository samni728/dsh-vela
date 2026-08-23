from __future__ import annotations

from dsh_novel.providers.base import WriterRequest


class DeterministicFakeProvider:
    """Deterministic provider for contract tests and offline smoke runs."""

    name = "deterministic_fake"

    def generate_chapter(self, request: WriterRequest) -> str:
        contract = request.contract
        events = contract.required_events or [contract.purpose]
        event_text = "、".join(events)
        variants = [
            (
                "晨雾沿着石阶缓慢退去，最先显露的是门前那道新鲜划痕。",
                "沉默被一声短促的询问打破，所有人的注意力随之改变。",
            ),
            (
                "雨点敲过窗棂，屋内的地图却比天气更令人不安。",
                "一份迟到的消息让原先稳固的判断出现了裂缝。",
            ),
            (
                "暮色压低了街巷的轮廓，远处灯火依次亮起。",
                "看似寻常的相遇很快暴露出双方完全不同的目的。",
            ),
            (
                "风从空旷站台穿过，卷起一张无人认领的车票。",
                "旧日承诺被重新提起时，回答者第一次选择了回避。",
            ),
            (
                "钟声落下之后，会议室里仍没有人率先离席。",
                "被忽略的细节重新排成线索，迫使众人修正计划。",
            ),
            (
                "河面反光刺得人睁不开眼，岸边脚印却异常清楚。",
                "追踪者在岔路前停下，因为证据指向了意料之外的人。",
            ),
            (
                "档案柜最底层传来轻响，一枚生锈钥匙落在地上。",
                "尘封记录补全了缺口，也让原有解释变得站不住脚。",
            ),
            (
                "夜班列车驶过城北，震动使杯中的水泛起细纹。",
                "行动开始前，主角主动改变了约定好的分工。",
            ),
        ]
        opening, turn = variants[(contract.chapter_number - 1) % len(variants)]
        return "\n\n".join(
            [
                f"# {contract.title}",
                f"{opening}第{contract.chapter_number}章围绕{contract.purpose}展开。",
                turn,
                f"{turn}由此推动了{event_text}。每一步都改变了下一步的条件，"
                "人物没有依靠偶然跳过矛盾，而是在行动中承担了后果。",
                f"回望{opening}这一开端，冲突抵达临界点后，"
                f"第{contract.chapter_number}章的问题得到暂时回应，新的不确定性也随之出现。",
                contract.handoff
                or f"章节结束时，人物已经完成第{contract.chapter_number}章的选择，并准备继续前行。",
            ]
        )
