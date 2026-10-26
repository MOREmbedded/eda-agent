# SPDX-License-Identifier: Apache-2.0
"""Regression: a 125% desktop must not turn Tools into Reports."""
import pytest
from types import SimpleNamespace
from eda_agent.ui import menu


class Cursor:
    def __init__(self, refuse=False, clipped=False):
        self.position = (-240, 300)
        self.events = []
        self.refuse = refuse
        self.clipped = clipped

    def GetPhysicalCursorPos(self, pointer):
        pointer._obj.x, pointer._obj.y = self.position
        return 1

    def SetPhysicalCursorPos(self, x, y):
        if self.refuse and (x,y) != (-240,300):
            return 0
        self.position = ((0,0) if self.clipped and (x,y) != (-240,300) else (x,y))
        return 1

    def GetCursorPos(self, pointer):
        raise AssertionError('Logical coordinates are not accessibility pixels')

    def SetCursorPos(self, x, y):
        # This is the wrong position on the user's 125% desktop.
        self.position = (round(x*1.25),round(y*1.25))

    def mouse_event(self, flag, *_):
        self.events.append((flag,self.position))


def test_click_uses_physical_pixels_and_restores_negative_monitor_position(monkeypatch):
    cursor=Cursor()
    monkeypatch.setattr(menu,'_u',lambda:cursor)
    monkeypatch.setattr(menu.time,'sleep',lambda _:None)
    monkeypatch.setattr(menu,'frame',lambda _pid:SimpleNamespace(hwnd=1))
    monkeypatch.setattr(menu.win,'require_foreground',lambda *_:None)
    menu._click(123,330,43)
    assert cursor.events == [(menu._MOUSE_DOWN,(330,43)),(menu._MOUSE_UP,(330,43))]
    assert cursor.position == (-240,300)


@pytest.mark.parametrize('fail',[False,True])
def test_worker_dpi_context_is_restored_even_when_menu_operation_fails(monkeypatch, fail):
    calls=[]
    def set_context(value):
        calls.append(value.value if hasattr(value,'value') else value)
        return 123
    monkeypatch.setattr(menu,'_u',lambda:SimpleNamespace(SetThreadDpiAwarenessContext=set_context))
    def operation():
        with menu._physical_pixels():
            assert calls == [menu.ctypes.c_void_p(-4).value]
            if fail:
                raise RuntimeError('menu failure')
    if fail:
        with pytest.raises(RuntimeError,match='menu failure'):
            operation()
    else:
        operation()
    assert calls == [menu.ctypes.c_void_p(-4).value,123]


@pytest.mark.parametrize('mode',['refuse','clipped'])
def test_failed_or_clipped_move_never_clicks_another_control(monkeypatch, mode):
    cursor=Cursor(**{mode:True})
    monkeypatch.setattr(menu,'_u',lambda:cursor)
    monkeypatch.setattr(menu.time,'sleep',lambda _:None)
    monkeypatch.setattr(menu,'frame',lambda _pid:SimpleNamespace(hwnd=1))
    monkeypatch.setattr(menu.win,'require_foreground',lambda *_:None)
    with pytest.raises(OSError,match='no menu click sent'):
        menu._click(123,330,43)
    assert cursor.events == []
    assert cursor.position == (-240,300)
