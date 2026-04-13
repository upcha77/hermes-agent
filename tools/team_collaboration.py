#!/usr/bin/env python3
"""
Team Collaboration System for Hermes

레퍼런스: 멀티 에이전트 하네스 설계 (wikidocs) - Team Agents 지원
- 세션 공유
- 설정 동기화
- 충돌 감지/해결
"""

import json
import logging
import os
import time
import hashlib
from pathlib import Path

# SQLite backend for thread-safe storage
try:
    from tools.storage.sqlite_backend import SQLiteBackend, TeamSessionStore
    SQLITE_AVAILABLE = True
except ImportError:
    SQLITE_AVAILABLE = False
    logger.warning("SQLite backend not available, falling back to JSON")
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class SharedSession:
    """공유 세션 정보"""
    session_id: str
    owner: str  # Telegram ID, Discord ID, etc.
    platform: str  # telegram, discord, slack
    created_at: float
    last_activity: float
    context: Dict[str, Any] = field(default_factory=dict)
    collaborators: List[str] = field(default_factory=list)
    status: str = "active"  # active, paused, archived
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ConfigChange:
    """설정 변경 이력"""
    timestamp: float
    user: str
    key: str
    old_value: Any
    new_value: Any
    checksum: str
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class Conflict:
    """충돌 정보"""
    conflict_id: str
    session_id: str
    type: str  # file, config, state
    description: str
    parties: List[str]
    detected_at: float
    resolved: bool = False
    resolution: Optional[str] = None
    
    def to_dict(self) -> Dict:
        return asdict(self)


class TeamSessionManager:
    """
    팀 세션 관리자
    
    다중 사용자 세션 공유 및 협업 지원
    - SQLite 모드: TeamSessionStore를 통해 thread-safe 원자적 연산
    - JSON 모드: 레거시 호환 (single-thread 전용)
    """
    
    def __init__(self, storage_path: Optional[str] = None, use_sqlite: bool = True):
        # SQLite backend for thread-safe concurrent access
        if use_sqlite and SQLITE_AVAILABLE:
            db_path = os.path.expanduser("~/.hermes/team_sessions.db")
            backend = SQLiteBackend(db_path)
            self.store = TeamSessionStore(backend)
            # Migrate from JSON if needed
            backend.migrate_from_json(
                os.path.expanduser("~/.hermes/team_sessions/sessions.json")
            )
            self.use_sqlite = True
        else:
            # Fallback to JSON (legacy, not thread-safe)
            self.store = None
            self.storage_path = Path(storage_path or os.path.expanduser("~/.hermes/team_sessions"))
            self.storage_path.mkdir(parents=True, exist_ok=True)
            self.sessions: Dict[str, SharedSession] = {}
            self._load_sessions()
            self.use_sqlite = False
    
    def _load_sessions(self):
        """저장된 세션 로드 (JSON 모드 전용)"""
        sessions_file = self.storage_path / "sessions.json"
        if sessions_file.exists():
            try:
                with open(sessions_file, 'r') as f:
                    data = json.load(f)
                    for session_id, session_data in data.items():
                        self.sessions[session_id] = SharedSession(**session_data)
            except Exception as e:
                logger.warning(f"Failed to load sessions: {e}")
    
    def _save_sessions(self):
        """세션 저장 (JSON 모드 전용)"""
        sessions_file = self.storage_path / "sessions.json"
        data = {sid: s.to_dict() for sid, s in self.sessions.items()}
        with open(sessions_file, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    
    def create_session(
        self,
        owner: str,
        platform: str,
        initial_context: Optional[Dict] = None
    ) -> SharedSession:
        """
        새로운 공유 세션 생성
        
        Args:
            owner: 세션 소유자 ID
            platform: 메시징 플랫폼 (telegram, discord, slack)
            initial_context: 초기 컨텍스트
        """
        timestamp = time.time()
        session_id = f"team_{platform}_{owner}_{int(timestamp)}"
        
        if self.use_sqlite:
            # SQLite: 원자적 생성
            self.store.create_session(
                session_id=session_id,
                owner=owner,
                platform=platform,
                context=initial_context
            )
            logger.info(f"Created team session (SQLite): {session_id}")
        else:
            # JSON 레거시
            session = SharedSession(
                session_id=session_id,
                owner=owner,
                platform=platform,
                created_at=timestamp,
                last_activity=timestamp,
                context=initial_context or {},
                collaborators=[owner],
                status="active"
            )
            self.sessions[session_id] = session
            self._save_sessions()
            logger.info(f"Created team session (JSON): {session_id}")
        
        # 반환용 SharedSession 객체 생성
        return SharedSession(
            session_id=session_id,
            owner=owner,
            platform=platform,
            created_at=timestamp,
            last_activity=timestamp,
            context=initial_context or {},
            collaborators=[owner],
            status="active"
        )
    
    def join_session(self, session_id: str, user: str) -> bool:
        """세션에 참여"""
        if self.use_sqlite:
            return self.store.add_collaborator(session_id, user)
        
        # JSON 레거시
        if session_id not in self.sessions:
            return False
        
        session = self.sessions[session_id]
        if user not in session.collaborators:
            session.collaborators.append(user)
            session.last_activity = time.time()
            self._save_sessions()
            logger.info(f"User {user} joined session {session_id}")
        return True
    
    def leave_session(self, session_id: str, user: str) -> bool:
        """세션에서 퇴장"""
        if self.use_sqlite:
            # SQLite: collaborator를 제거하는 전용 메서드가 없으므로
            # context update를 통해 처리 (향후 remove_collaborator 추가 가능)
            session_data = self.store.get_session(session_id)
            if not session_data:
                return False
            collaborators = session_data.get("collaborators", [])
            if user in collaborators:
                collaborators.remove(user)
                # owner가 나가면 archived
                if user == session_data.get("owner"):
                    self.store.archive_session(session_id, user)
                else:
                    self.store.update_context(session_id, {"_collaborators_updated": True})
            return True
        
        # JSON 레거시
        if session_id not in self.sessions:
            return False
        
        session = self.sessions[session_id]
        if user in session.collaborators:
            session.collaborators.remove(user)
            session.last_activity = time.time()
            
            # 소유자가 나가면 세션 종료
            if user == session.owner:
                session.status = "archived"
            
            self._save_sessions()
            logger.info(f"User {user} left session {session_id}")
        return True
    
    def update_context(
        self,
        session_id: str,
        context_update: Dict[str, Any],
        user: str
    ) -> bool:
        """세션 컨텍스트 업데이트"""
        if self.use_sqlite:
            # SQLite: Optimistic Locking으로 원자적 업데이트
            success, _ = self.store.update_context(session_id, context_update)
            if success:
                logger.info(f"Context updated for session {session_id} by {user} (SQLite)")
            return success
        
        # JSON 레거시
        if session_id not in self.sessions:
            return False
        
        session = self.sessions[session_id]
        if user not in session.collaborators:
            return False
        
        # Deep merge
        self._deep_update(session.context, context_update)
        session.last_activity = time.time()
        self._save_sessions()
        
        logger.info(f"Context updated for session {session_id} by {user}")
        return True
    
    def _deep_update(self, base: Dict, update: Dict):
        """중첩 dict 업데이트"""
        for key, value in update.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_update(base[key], value)
            else:
                base[key] = value
    
    def get_session(self, session_id: str) -> Optional[SharedSession]:
        """세션 조회"""
        if self.use_sqlite:
            data = self.store.get_session(session_id)
            if not data:
                return None
            return SharedSession(
                session_id=data["session_id"],
                owner=data["owner"],
                platform=data["platform"],
                created_at=data["created_at"],
                last_activity=data["last_activity"],
                context=data.get("context", {}),
                collaborators=data.get("collaborators", []),
                status=data["status"]
            )
        
        # JSON 레거시
        return self.sessions.get(session_id)
    
    def list_user_sessions(self, user: str) -> List[SharedSession]:
        """사용자의 세션 목록"""
        if self.use_sqlite:
            rows = self.store.list_user_sessions(user)
            return [
                SharedSession(
                    session_id=r["session_id"],
                    owner=r["owner"],
                    platform=r["platform"],
                    created_at=r["created_at"],
                    last_activity=r["last_activity"],
                    context=r.get("context", {}),
                    collaborators=r.get("collaborators", []),
                    status=r["status"]
                )
                for r in rows
            ]
        
        # JSON 레거시
        return [
            s for s in self.sessions.values()
            if user in s.collaborators and s.status == "active"
        ]
    
    def archive_session(self, session_id: str, user: str) -> bool:
        """세션 아카이브"""
        if self.use_sqlite:
            return self.store.archive_session(session_id, user)
        
        # JSON 레거시
        if session_id not in self.sessions:
            return False
        
        session = self.sessions[session_id]
        if user != session.owner:
            return False
        
        session.status = "archived"
        self._save_sessions()
        logger.info(f"Session {session_id} archived")
        return True


class TeamConfigSync:
    """
    팀 설정 동기화
    
    설정 변경 추적 및 동기화
    """
    
    def __init__(self, storage_path: Optional[str] = None):
        self.storage_path = Path(storage_path or os.path.expanduser("~/.hermes/team_config"))
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.changes: List[ConfigChange] = []
        self._load_changes()
    
    def _load_changes(self):
        """변경 이력 로드"""
        changes_file = self.storage_path / "changes.json"
        if changes_file.exists():
            try:
                with open(changes_file, 'r') as f:
                    data = json.load(f)
                    self.changes = [ConfigChange(**c) for c in data]
            except Exception as e:
                logger.warning(f"Failed to load config changes: {e}")
    
    def _save_changes(self):
        """변경 이력 저장"""
        changes_file = self.storage_path / "changes.json"
        data = [c.to_dict() for c in self.changes]
        with open(changes_file, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    
    def record_change(
        self,
        user: str,
        key: str,
        old_value: Any,
        new_value: Any
    ) -> ConfigChange:
        """
        설정 변경 기록
        
        Args:
            user: 변경한 사용자
            key: 설정 키 (dot notation, e.g., "model.default")
            old_value: 이전 값
            new_value: 새 값
        """
        # 값의 checksum 생성
        checksum = hashlib.md5(
            json.dumps(new_value, sort_keys=True, default=str).encode()
        ).hexdigest()[:8]
        
        change = ConfigChange(
            timestamp=time.time(),
            user=user,
            key=key,
            old_value=old_value,
            new_value=new_value,
            checksum=checksum
        )
        
        self.changes.append(change)
        self._save_changes()
        
        logger.info(f"Config change recorded: {key} by {user}")
        return change
    
    def get_recent_changes(
        self,
        since: Optional[float] = None,
        user: Optional[str] = None,
        key_prefix: Optional[str] = None
    ) -> List[ConfigChange]:
        """최근 변경 조회"""
        since = since or (time.time() - 86400)  # 기본 24시간
        
        filtered = [
            c for c in self.changes
            if c.timestamp >= since
        ]
        
        if user:
            filtered = [c for c in filtered if c.user == user]
        
        if key_prefix:
            filtered = [c for c in filtered if c.key.startswith(key_prefix)]
        
        return sorted(filtered, key=lambda c: c.timestamp, reverse=True)
    
    def sync_config_to_session(
        self,
        session_id: str,
        config_keys: List[str],
        session_manager: TeamSessionManager
    ) -> bool:
        """
        설정을 세션에 동기화
        
        Args:
            session_id: 대상 세션
            config_keys: 동기화할 설정 키 목록
            session_manager: 세션 관리자
        """
        session = session_manager.get_session(session_id)
        if not session:
            return False
        
        # 최근 변경 조회
        changes = self.get_recent_changes(key_prefix=config_keys[0].split('.')[0])
        
        # 세션 컨텍스트에 반영
        if 'config_sync' not in session.context:
            session.context['config_sync'] = {}
        
        for change in changes:
            session.context['config_sync'][change.key] = {
                'value': change.new_value,
                'updated_by': change.user,
                'updated_at': change.timestamp,
                'checksum': change.checksum
            }
        
        session_manager._save_sessions()
        return True


class TeamConflictResolver:
    """
    팀 충돌 감지 및 해결
    
    동시 편집, 설정 충돌 등을 감지하고 해결
    """
    
    def __init__(self, storage_path: Optional[str] = None):
        self.storage_path = Path(storage_path or os.path.expanduser("~/.hermes/team_conflicts"))
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.conflicts: Dict[str, Conflict] = {}
        self._load_conflicts()
    
    def _load_conflicts(self):
        """충돌 로드"""
        conflicts_file = self.storage_path / "conflicts.json"
        if conflicts_file.exists():
            try:
                with open(conflicts_file, 'r') as f:
                    data = json.load(f)
                    for cid, conflict_data in data.items():
                        self.conflicts[cid] = Conflict(**conflict_data)
            except Exception as e:
                logger.warning(f"Failed to load conflicts: {e}")
    
    def _save_conflicts(self):
        """충돌 저장"""
        conflicts_file = self.storage_path / "conflicts.json"
        data = {cid: c.to_dict() for cid, c in self.conflicts.items()}
        with open(conflicts_file, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    
    def detect_conflict(
        self,
        session_id: str,
        conflict_type: str,
        description: str,
        parties: List[str]
    ) -> Optional[Conflict]:
        """
        충돌 감지 및 생성
        
        Args:
            session_id: 세션 ID
            conflict_type: 충돌 타입 (file, config, state)
            description: 설명
            parties: 관련 당사자 목록
        """
        # 중복 감지
        for conflict in self.conflicts.values():
            if (conflict.session_id == session_id and 
                conflict.type == conflict_type and
                not conflict.resolved):
                # 기존 미해결 충돌 존재
                return conflict
        
        # 새 충돌 생성
        conflict_id = f"conflict_{int(time.time())}_{hashlib.md5(description.encode()).hexdigest()[:8]}"
        
        conflict = Conflict(
            conflict_id=conflict_id,
            session_id=session_id,
            type=conflict_type,
            description=description,
            parties=parties,
            detected_at=time.time(),
            resolved=False
        )
        
        self.conflicts[conflict_id] = conflict
        self._save_conflicts()
        
        logger.warning(f"Conflict detected: {conflict_id} - {description}")
        return conflict
    
    def resolve_conflict(
        self,
        conflict_id: str,
        resolution: str,
        resolved_by: str
    ) -> bool:
        """
        충돌 해결
        
        Args:
            conflict_id: 충돌 ID
            resolution: 해결 방법 설명
            resolved_by: 해결한 사용자
        """
        if conflict_id not in self.conflicts:
            return False
        
        conflict = self.conflicts[conflict_id]
        conflict.resolved = True
        conflict.resolution = f"[{resolved_by}] {resolution}"
        self._save_conflicts()
        
        logger.info(f"Conflict resolved: {conflict_id} by {resolved_by}")
        return True
    
    def get_unresolved_conflicts(
        self,
        session_id: Optional[str] = None
    ) -> List[Conflict]:
        """미해결 충돌 조회"""
        conflicts = [c for c in self.conflicts.values() if not c.resolved]
        
        if session_id:
            conflicts = [c for c in conflicts if c.session_id == session_id]
        
        return sorted(conflicts, key=lambda c: c.detected_at, reverse=True)
    
    def check_file_conflict(
        self,
        session_id: str,
        file_path: str,
        users: List[str]
    ) -> Optional[Conflict]:
        """
        파일 편집 충돌 체크
        
        여러 사용자가 동일 파일을 편집하려 할 때 감지
        """
        return self.detect_conflict(
            session_id=session_id,
            conflict_type="file",
            description=f"Multiple users attempting to edit: {file_path}",
            parties=users
        )
    
    def check_config_conflict(
        self,
        session_id: str,
        config_key: str,
        users: List[str],
        values: List[Any]
    ) -> Optional[Conflict]:
        """
        설정 충돌 체크
        
        동일 설정을 다른 값으로 변경하려 할 때 감지
        """
        if len(set(str(v) for v in values)) > 1:
            return self.detect_conflict(
                session_id=session_id,
                conflict_type="config",
                description=f"Conflicting values for {config_key}: {values}",
                parties=users
            )
        return None


class TeamCollaborationHub:
    """
    팀 협업 중앙 허브
    
    세션, 설정, 충돌 관리 통합
    """
    
    def __init__(self, storage_base: Optional[str] = None):
        base = storage_base or os.path.expanduser("~/.hermes")
        self.session_manager = TeamSessionManager(f"{base}/team_sessions")
        self.config_sync = TeamConfigSync(f"{base}/team_config")
        self.conflict_resolver = TeamConflictResolver(f"{base}/team_conflicts")
    
    def create_team_session(
        self,
        owner: str,
        platform: str,
        context: Optional[Dict] = None
    ) -> Dict:
        """팀 세션 생성"""
        session = self.session_manager.create_session(owner, platform, context)
        return {
            "success": True,
            "session_id": session.session_id,
            "share_link": f"hermes://join/{session.session_id}",
            "invite_message": f"Join my Hermes session: /join {session.session_id}"
        }
    
    def join_team_session(self, session_id: str, user: str) -> Dict:
        """팀 세션 참여"""
        success = self.session_manager.join_session(session_id, user)
        session = self.session_manager.get_session(session_id)
        
        return {
            "success": success,
            "session_id": session_id,
            "collaborators": session.collaborators if session else [],
            "context": session.context if session else {}
        }
    
    def sync_team_config(
        self,
        session_id: str,
        user: str,
        config_updates: Dict[str, Any]
    ) -> Dict:
        """팀 설정 동기화"""
        # 충돌 체크
        session = self.session_manager.get_session(session_id)
        if not session:
            return {"success": False, "error": "Session not found"}
        
        conflicts = []
        for key, new_value in config_updates.items():
            # 설정 변경 기록
            old_value = session.context.get('config', {}).get(key)
            self.config_sync.record_change(user, key, old_value, new_value)
            
            # 충돌 감지
            if old_value is not None and old_value != new_value:
                conflict = self.conflict_resolver.check_config_conflict(
                    session_id, key, [user], [old_value, new_value]
                )
                if conflict:
                    conflicts.append(conflict.to_dict())
        
        # 설정 업데이트
        if 'config' not in session.context:
            session.context['config'] = {}
        session.context['config'].update(config_updates)
        self.session_manager._save_sessions()
        
        return {
            "success": True,
            "changes_recorded": len(config_updates),
            "conflicts_detected": len(conflicts),
            "conflicts": conflicts
        }
    
    def get_team_status(self, session_id: str) -> Dict:
        """팀 상태 조회"""
        session = self.session_manager.get_session(session_id)
        if not session:
            return {"success": False, "error": "Session not found"}
        
        unresolved = self.conflict_resolver.get_unresolved_conflicts(session_id)
        recent_changes = self.config_sync.get_recent_changes()
        
        return {
            "success": True,
            "session": session.to_dict(),
            "active_collaborators": len([c for c in session.collaborators if session.status == "active"]),
            "unresolved_conflicts": len(unresolved),
            "recent_changes": len(recent_changes),
            "conflict_details": [c.to_dict() for c in unresolved]
        }


# Convenience functions
def create_team_session(
    owner: str,
    platform: str = "telegram",
    context: Optional[Dict] = None
) -> str:
    """팀 세션 생성 (편의 함수)"""
    hub = TeamCollaborationHub()
    result = hub.create_team_session(owner, platform, context)
    if result["success"]:
        return result["session_id"]
    return ""


def join_team_session(session_id: str, user: str) -> bool:
    """팀 세션 참여 (편의 함수)"""
    hub = TeamCollaborationHub()
    result = hub.join_team_session(session_id, user)
    return result["success"]


# Register with Hermes tool registry
try:
    from tools.registry import registry
    
    def _check_team_collaboration() -> bool:
        return True
    
    hub = TeamCollaborationHub()
    
    registry.register(
        name="team_create_session",
        toolset="collaboration",
        schema={
            "name": "team_create_session",
            "description": "Create a team collaboration session for multi-user agent work",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Session owner ID"},
                    "platform": {"type": "string", "enum": ["telegram", "discord", "slack"], "default": "telegram"},
                    "context": {"type": "object", "description": "Initial session context"}
                },
                "required": ["owner"]
            }
        },
        handler=lambda args, **kw: json.dumps(hub.create_team_session(
            args.get("owner"),
            args.get("platform", "telegram"),
            args.get("context")
        ), ensure_ascii=False),
        check_fn=_check_team_collaboration,
        description="Create team session for multi-user collaboration",
        emoji="👥",
    )
    
    registry.register(
        name="team_join_session",
        toolset="collaboration",
        schema={
            "name": "team_join_session",
            "description": "Join an existing team collaboration session",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session ID to join"},
                    "user": {"type": "string", "description": "User ID joining"}
                },
                "required": ["session_id", "user"]
            }
        },
        handler=lambda args, **kw: json.dumps(hub.join_team_session(
            args.get("session_id"),
            args.get("user")
        ), ensure_ascii=False),
        check_fn=_check_team_collaboration,
        description="Join team collaboration session",
        emoji="🤝",
    )
    
    registry.register(
        name="team_sync_config",
        toolset="collaboration",
        schema={
            "name": "team_sync_config",
            "description": "Sync configuration changes to team session with conflict detection",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "user": {"type": "string"},
                    "config_updates": {"type": "object"}
                },
                "required": ["session_id", "user", "config_updates"]
            }
        },
        handler=lambda args, **kw: json.dumps(hub.sync_team_config(
            args.get("session_id"),
            args.get("user"),
            args.get("config_updates")
        ), ensure_ascii=False),
        check_fn=_check_team_collaboration,
        description="Sync team config with conflict detection",
        emoji="🔄",
    )
    
    registry.register(
        name="team_get_status",
        toolset="collaboration",
        schema={
            "name": "team_get_status",
            "description": "Get team session status including conflicts and recent changes",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"}
                },
                "required": ["session_id"]
            }
        },
        handler=lambda args, **kw: json.dumps(hub.get_team_status(
            args.get("session_id")
        ), ensure_ascii=False),
        check_fn=_check_team_collaboration,
        description="Get team session status",
        emoji="📊",
    )
    
except ImportError:
    pass  # Registry not available during import
