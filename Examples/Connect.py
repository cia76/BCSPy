import logging  # Выводим лог на консоль и в файл
from datetime import datetime  # Дата и время

from BCSPy.BCSPy import BCSPy


def on_new_bar(response):  # Обработчик события прихода нового бара
    global last_bar, dt_last_bar  # Последний полученный бар и его дата/время
    dt_bar_close = bp_provider.utc_to_msk_datetime(datetime.fromisoformat(response['dateTime'][:-1]))  # БКС передает дату/время закрытия бара
    if dt_last_bar is not None and dt_last_bar < dt_bar_close:  # Если время бара стало больше (предыдущий бар закрыт, новый бар открыт)
        logger.info(f'{dt_bar_close:%d.%m.%Y %H:%M} '
                    f'O:{last_bar["open"]} '
                    f'H:{last_bar["high"]} '
                    f'L:{last_bar["low"]} '
                    f'C:{last_bar["close"]} '
                    f'V:{int(last_bar["volume"])}')
    last_bar = response  # Запоминаем бар
    dt_last_bar = dt_bar_close  # Запоминаем дату и время бара


if __name__ == '__main__':  # Точка входа при запуске этого скрипта
    logger = logging.getLogger('BCSPy.Connect')  # Будем вести лог
    # bp_provider = BCSPy('<Токен>')  # При первом подключении нужно передать токен
    bp_provider = BCSPy()  # Подключаемся ко всем торговым счетам

    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',  # Формат сообщения
                        datefmt='%d.%m.%Y %H:%M:%S',  # Формат даты
                        level=logging.INFO,  # Уровень логируемых событий NOTSET/DEBUG/INFO/WARNING/ERROR/CRITICAL
                        handlers=[logging.FileHandler('Connect.log', encoding='utf-8'), logging.StreamHandler()])  # Лог записываем в файл и выводим на консоль
    logging.Formatter.converter = lambda *args: datetime.now(tz=bp_provider.tz_msk).timetuple()  # В логе время указываем по МСК
    logging.getLogger('urllib3').setLevel(logging.CRITICAL + 1)  # Не пропускать в лог
    logging.getLogger('websockets').setLevel(logging.CRITICAL + 1)  # события в этих библиотеках

    dataname = 'TQBR.SBER'  # Тикер
    tf = 'M1'  # Временной интервал

    # Проверяем работу запрос/ответ
    class_code, ticker = bp_provider.dataname_to_class_code_ticker(dataname)  # Код режима торгов и тикер из названия тикера
    logger.debug(bp_provider.get_instrument_ticker([ticker]))

    # Проверяем работу подписок
    logger.info(f'Подписка на {tf} бары тикера {dataname}')
    bp_provider.on_candle.subscribe(on_new_bar)  # Обработчик события прихода нового бара
    last_bar: dict  # Последнего полученного бара пока нет
    dt_last_bar = None  # И даты/времени у него пока нет
    bp_provider.subscribe_last_candles(0, [{'classCode': class_code, 'ticker': ticker}], tf)  # Подписываемся на новые бары

    # Выход
    input('Enter - выход\n')
    bp_provider.on_candle.unsubscribe(on_new_bar)  # Отменяем подписку на новые бары
    bp_provider.close_web_socket()  # Перед выходом закрываем соединение с WebSocket
