"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import DomainRules


# 随转办单一并冻结的原补件期限字段。
DEADLINE_SNAPSHOT_FIELDS = (
    "deadline_day",
    "days_remaining",
    "response_day",
    "evidence_request_day",
    "evidence_due_day",
)


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _require_office(actor: Actor) -> str:
        office = actor.organization.strip() if actor.organization else ""
        if not office:
            raise PermissionDenied("缺少办事处身份(X-Org)")
        return office

    @staticmethod
    def _deadline_snapshot(payload: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = {key: payload[key] for key in DEADLINE_SNAPSHOT_FIELDS if key in payload}
        # 补件期限可能尚未产生，但原提交期限始终可查。
        snapshot.setdefault("deadline_day", payload.get("deadline_day"))
        return snapshot

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(
            reference, self.rules.INITIAL_STATE, prepared, actor.user_id,
            owning_office=actor.organization.strip() or None,
        )

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        # 提前给出清晰提示；仓储层在同一事务内再次拦截以消除并发竞态。
        pending = self.repository.get_pending_transfer(record_id)
        if pending is not None and action in {"submit", "decide"}:
            raise Conflict("转办确认前双方均不能提交或决定")
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    # ---- 两阶段跨办事处转办 ----

    def initiate_transfer(self, actor: Actor, record_id: int, expected_version: int,
                         to_office: str) -> Dict[str, Any]:
        """第一阶段：转出方按当前修订号发起，冻结原补件期限。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_transfer(actor.role):
            raise PermissionDenied("角色无权发起转办")
        from_office = self._require_office(actor)
        to_office = text({"to_office": to_office}, "to_office")
        if to_office == from_office:
            raise ValidationError("接收办事处必须与转出办事处不同")
        if not isinstance(expected_version, int):
            raise ValidationError("expected_version必须是整数")
        record = self.repository.get(record_id)
        if record.get("owning_office") and record["owning_office"] != from_office:
            raise PermissionDenied("案件当前不属于该办事处，无法发起转办")
        snapshot = self._deadline_snapshot(record["payload"])
        return self.repository.create_transfer(
            record_id=record_id,
            expected_version=int(expected_version),
            from_office=from_office,
            to_office=to_office,
            deadline_snapshot=snapshot,
            actor_id=actor.user_id,
        )

    def confirm_transfer(self, actor: Actor, transfer_id: int) -> Dict[str, Any]:
        """第二阶段：接收方确认，归属一次切换；重复确认返回同一结果。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_transfer(actor.role):
            raise PermissionDenied("角色无权确认转办")
        to_office = self._require_office(actor)
        transfer = self.repository.get_transfer(transfer_id)
        # 已确认的单据对重复确认幂等返回，不再校验办事处归属。
        if transfer["status"] == "pending" and transfer["to_office"] != to_office:
            raise PermissionDenied("该转办单不属于当前接收办事处")
        return self.repository.confirm_transfer(transfer_id, actor.user_id)

    def get_transfer(self, actor: Actor, transfer_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_transfer(transfer_id)

    def list_transfers(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_transfers(record_id)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        by_state = self.repository.stats()
        by_office = self.repository.stats_by_office()
        result = dict(by_state)
        result["total"] = sum(by_state.values())
        result["by_office"] = by_office
        return result
