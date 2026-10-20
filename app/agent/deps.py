from dataclasses import dataclass
from app.agent.planner import Planner
from app.browser.tools import BrowserTools
from app.testing.compiler import TestCompiler
from app.testing.replay import ReplayRunner
from app.storage import RunStore


@dataclass
class AgentDeps:
    planner: Planner
    browser: BrowserTools
    compiler: TestCompiler
    replay: ReplayRunner
    store: RunStore
