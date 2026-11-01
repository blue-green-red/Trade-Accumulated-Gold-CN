"""Open execution fixtures on weekends; genuine trading-hours boundary tests stay unpatched."""
import sys,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
class Result(unittest.TextTestResult):
 def startTest(self,test):
  # Generic execution fixtures assert a tradable snapshot. Keep real clock ages and
  # browser dates, while dedicated session-boundary tests retain the genuine rule.
  self.session_patch=None
  if test.__class__.__module__!='test_trading_hours':
   self.session_patch=patch('monitor.execution.trading_session',return_value={'is_open':True})
   self.session_patch.start()
  super().startTest(test)
 def stopTest(self,test):
  if self.session_patch:self.session_patch.stop()
  super().stopTest(test)
suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
result=unittest.TextTestRunner(verbosity=2,resultclass=Result).run(suite)
sys.exit(not result.wasSuccessful())
