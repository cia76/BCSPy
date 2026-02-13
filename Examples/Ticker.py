import logging  # Выводим лог на консоль и в файл
from datetime import date, timedelta, datetime  # Дата и время

from BCSPy.BCSPy import BCSPy


def get_future_on_date(base, future_date=date.today()):  # Фьючерсный контракт на дату
    if future_date.day > 15 and future_date.month in (3, 6, 9, 12):  # Если нужно переходить на следующий фьючерс
        future_date += timedelta(days=30)  # то добавляем месяц к дате
    period = 'H' if future_date.month <= 3 else 'M' if future_date.month <= 6 else 'U' if future_date.month <= 9 else 'Z'  # Месяц экспирации: 3-H, 6-M, 9-U, 12-Z
    digit = future_date.year % 10  # Последняя цифра года
    return f'SPBFUT.{base}{period}{digit}'


if __name__ == '__main__':  # Точка входа при запуске этого скрипта
    logger = logging.getLogger('BCSPy.Ticker')  # Будем вести лог
    bp_provider = BCSPy()  # Подключаемся ко всем торговым счетам

    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',  # Формат сообщения
                        datefmt='%d.%m.%Y %H:%M:%S',  # Формат даты
                        level=logging.INFO,  # Уровень логируемых событий NOTSET/DEBUG/INFO/WARNING/ERROR/CRITICAL
                        handlers=[logging.FileHandler('Ticker.log', encoding='utf-8'), logging.StreamHandler()])  # Лог записываем в файл и выводим на консоль
    logging.Formatter.converter = lambda *args: datetime.now(tz=bp_provider.tz_msk).timetuple()  # В логе время указываем по МСК

    datanames = ('TQBR.SBER', 'TQBR.HYDR', get_future_on_date("Si"), get_future_on_date("RI"), 'SPBFUT.CNYRUBF', 'SPBFUT.IMOEXF')  # Кортеж тикеров

    for dataname in datanames:  # Пробегаемся по всем тикерам
        class_code, ticker = bp_provider.dataname_to_class_code_ticker(dataname)  # Код режима торгов и тикер
        si = next((instrument for instrument in bp_provider.get_instrument_ticker([ticker]) if instrument['boards'][0]['classCode'] == class_code), None)  # Информация о тикере
        if si is None:  # Если тикер не найден
            logger.error(f'Тикер {class_code}.{ticker} не найден')
            continue  # Переходим к следующему тикеру
        logger.info(f'Информация о тикере {class_code}.{ticker} ({si["displayName"]}, {si["instrumentType"]}) на бирже {si["boards"][0]["exchange"]}')
        logger.info(f'- Лот: {int(float(si["lotSize"]))}')
        logger.info(f'- Шаг цены: {si["minimumStep"]}')
        logger.info(f'- Кол-во десятичных знаков: {si["scale"]}')
    bp_provider.close_web_socket()  # Перед выходом закрываем соединение с WebSocket
