"""Relógio parado para os testes.

Os módulos leem "hoje" com datetime.now() / date.today(). Testes com dados de
uma data fixa quebravam a cada virada de mês: os lançamentos de junho a agosto
saíam das janelas de cálculo. `congelar` troca datetime e date, nos módulos
indicados, por versões que devolvem sempre o mesmo instante.
"""
from datetime import date as _date, datetime as _datetime
from unittest.mock import patch


def congelar(caso, quando: str, modulos) -> None:
    fixo = _datetime.fromisoformat(quando)

    class DataFixa(_date):
        @classmethod
        def today(cls):
            return cls(fixo.year, fixo.month, fixo.day)

    class DataHoraFixa(_datetime):
        @classmethod
        def now(cls, tz=None):
            return fixo if tz is None else fixo.replace(tzinfo=tz)

        @classmethod
        def today(cls):
            return fixo

    for modulo in modulos:
        for nome, falso in (("datetime", DataHoraFixa), ("date", DataFixa)):
            if isinstance(getattr(modulo, nome, None), type):
                p = patch.object(modulo, nome, falso)
                p.start()
                caso.addCleanup(p.stop)
