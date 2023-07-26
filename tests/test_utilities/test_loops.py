import pytest
import time

from opensourceleg.utilities import LoopKiller, SoftRealtimeLoop
from tests.test_joints.test_joint import patch_time_time


def test_loopkiller_init():

    """
    Tests the LoopKiller constructor\n
    Initializes a default LoopKiller object and asserts the attribute values
    are set properly. Then initializes a LoopKiller object with a fade_time
    and asserts the attribute values are set properly.
    """

    lk = LoopKiller()
    assert lk._fade_time == 0.0
    assert lk._soft_kill_time == None
    lk1 = LoopKiller(fade_time=1.0)
    assert lk1._fade_time == 1.0
    assert lk1._soft_kill_time == None
    assert lk1._kill_now == False
    assert lk1._kill_soon == False


def test_loopkiller_handle_signal():

    """
    Tests the LoopKiller handle_signal method\n
    Initializes a default LoopKiller object and asserts the kill_now attribute
    is set to false. Then calls the handle_signal method with arguments set to
    none and asserts the kill_now attribute is set to true.
    """

    lkhs = LoopKiller()
    assert lkhs.kill_now == False
    lkhs.handle_signal(signum=None, frame=None)
    assert lkhs.kill_now == True

@pytest.fixture
def patch_time_time2(monkeypatch):

    """
    Fixture to patch the time.time method\n
    Patches the time.time method to return a list of values one at a time.
    """

    values = [0, 1, 2, 3, 4, 5]
    monkeypatch.setattr(time, "time", lambda: values.pop(0))


def test_loopkiller_get_fade(patch_time_time2):

    """
    Tests the LoopKiller get_fade method\n
    Initializes a LoopKiller object with a fade_time and asserts the get_fade
    method returns the correct value. Then sets the soft_kill_time attribute
    and asserts the get_fade method returns the correct value. Then calls the
    get_fade method again and asserts the get_fade method returns 0.0.
    """

    lkgf = LoopKiller(fade_time=1.0)
    assert lkgf.get_fade() == 1.0
    lkgf._kill_soon = True
    lkgf._soft_kill_time = 0.0
    assert lkgf.get_fade() == 1.0
    assert lkgf.get_fade() == 0.0


def test_loopkiller_kill_now_prop(patch_time_time2):
    lkknp = LoopKiller(fade_time=1.0)
    lkknp._kill_soon = True
    lkknp._soft_kill_time = 0.0
    assert lkknp.kill_now == False
    lkknp._soft_kill_time = 0.5
    assert lkknp.kill_now == False
    assert lkknp.kill_now == True
    assert lkknp.kill_now == True


def test_loopkiller_kill_now_setter(patch_time_time2):
    lkkns = LoopKiller(fade_time=1.0)
    lkkns._kill_now = True
    lkkns._kill_soon = True
    lkkns._soft_kill_time = 0.0
    lkkns.kill_now = False
    assert lkkns._kill_now == False
    assert lkkns._kill_soon == False
    assert lkkns._soft_kill_time == None
    lkkns.kill_now = True
    assert lkkns._kill_soon == True
    assert lkkns._soft_kill_time == 0.0
    assert lkkns._kill_now == False    
    lkkns.kill_now = True
    assert lkkns._kill_now == True
    lkkns.kill_now = False
    lkkns._fade_time = 0.0
    lkkns.kill_now = True
    assert lkkns._kill_now == True
    
    
def test_softrealtimeloop_init(patch_time_time2):
    srtl = SoftRealtimeLoop()
    assert srtl.t0 == 0.0
    assert srtl.t1 == 0.0
    assert isinstance(srtl.killer, LoopKiller)
    assert srtl.ttarg == None
    assert srtl.sum_err == 0.0
    assert srtl.sum_var == 0.0
    assert srtl.sleep_t_agg == 0.0
    assert srtl.n == 0
    assert srtl.report == False


def test_softrealtimeloop_stop(patch_time_time2):
    srtls = SoftRealtimeLoop()
    srtls.killer._kill_soon = True
    assert srtls.killer._kill_now == False
    srtls.stop()
    assert srtls.killer.kill_now == True
    

def test_softrealtimeloop_time(patch_time_time2):
    srtlt = SoftRealtimeLoop()
    assert srtlt.t0 == 0.0
    assert srtlt.time() == 1.0

def test_softrealtimeloop_time_since(patch_time_time2):
    srtls = SoftRealtimeLoop()
    assert srtls.t1 == 0.0
    assert srtls.time_since() == 1.0
