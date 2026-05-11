# Import all models here so Alembic can detect them
from app.models.user import User  # noqa: F401
from app.models.token import ApiToken  # noqa: F401
from app.models.project import Project  # noqa: F401
from app.models.permission import TokenProjectPermission  # noqa: F401
from app.models.milestone import Milestone  # noqa: F401
from app.models.phase import Phase  # noqa: F401
from app.models.task import Task  # noqa: F401
from app.models.task_note import TaskNote  # noqa: F401
from app.models.activity import ActivityEvent  # noqa: F401
from app.models.agent import Agent  # noqa: F401
from app.models.project_member import ProjectMember  # noqa: F401
from app.models.user_project_pin import UserProjectPin  # noqa: F401
from app.models.user_preferences import UserPreferences  # noqa: F401
from app.models.drift import DriftEscalation, DriftEvalAttempt  # noqa: F401
from app.models.system_settings import SystemSetting  # noqa: F401
