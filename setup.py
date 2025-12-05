from setuptools import setup, find_packages

setup(name='BCSPy',
      version='2025.12.01',  # Внутренняя версия формата <Год>.<Месяц>.<Номер>, т.к. БКС версии не ставит
      author='Чечет Игорь Александрович',
      description='Библиотека-обертка, которая позволяет работать с БКС торговое API брокера БКС из Python',
      url='https://github.com/cia76/BCSPy',
      packages=find_packages(),
      install_requires=[
            'keyring',  # Безопасное хранение торгового токена
            'requests',  # Запросы/ответы через HTTP API
            'urllib3',  # Соединение с сервером не установлено за максимальное кол-во попыток подключения
            'websockets',  # Управление подписками и заявками через WebSocket API
      ],
      python_requires='>=3.12',
      )
