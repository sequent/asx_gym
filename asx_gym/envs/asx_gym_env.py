import numpy as np
import io

from datetime import datetime, timedelta, date, time
import cv2
from gym.envs.classic_control import rendering

import matplotlib.pyplot as plt
from gym import Env
from gym import spaces, logger
from gym.utils import seeding
import sqlite3
from gym.utils.colorize import *
# Data manipulation packages
import pandas as pd
import pathlib
import mplfinance as mpf

date_fmt = '%Y-%m-%d'

plt.xticks(rotation=90)


# define a function which returns an image as numpy array from figure
def get_img_from_fig(fig, dpi=90):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, papertype='a4')
    buf.seek(0)
    img_arr = np.frombuffer(buf.getvalue(), dtype=np.uint8)
    buf.close()
    img = cv2.imdecode(img_arr, 1)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return img


class AsxGymEnv(Env):

    def __init__(self, **kwargs):
        self.fig, self.ax = plt.subplots()
        self.np_random, seed = seeding.np_random(0)
        seed = seeding.create_seed(32)
        self.seed(seed=seed)
        self.step_count = 0

        self.viewer = rendering.SimpleImageViewer()
        self.min_stock_date = date(2011, 1, 1)

        # default values
        self.user_set_start_date = kwargs.get('start_date', self.min_stock_date)
        if self.user_set_start_date < self.min_stock_date:
            self.user_set_start_date = self.min_stock_date
        self.start_date = self.user_set_start_date
        self.display_days = kwargs.get('display_days', 60)
        self.keep_same_company_when_reset = kwargs.get('keep_same_company_when_reset', True)
        self.keep_same_start_date_when_reset = kwargs.get('keep_same_start_date_when_reset', False)
        self.simulate_company_number = kwargs.get('simulate_company_number', -1)

        self.initial_fund = kwargs.get('initial_fund', 100000)
        self.expected_fund_increase_ratio = kwargs.get('expected_fund_increase_ratio', 2.0)
        self.expected_fund_decrease_ratio = kwargs.get('expected_fund_decrease_ratio', 0.2)

        # company index start from 1, 0 means empty slot
        self.max_company_number = 3000
        self.max_stock_price = 100000
        self.number_infinite = 10000000
        self.random_start_days = 100
        self.max_transaction_days = 0
        self.colors = ['b', 'g', 'r', 'c', 'm', 'y', 'k']
        self.color = self.colors[0]
        self.history_fund_values = [self.initial_fund] * 60

        # random start date
        offset_days = self.np_random.randint(0, self.random_start_days)
        self.start_date = self.user_set_start_date + timedelta(days=offset_days)

        self.action_space = spaces.Dict(
            {
                "company_id": spaces.Discrete(self.max_company_number),
                "buy_or_sell": spaces.Discrete(3),
                "volume": spaces.Box(np.float32(0), high=np.float32(self.number_infinite), dtype=np.float32),
                "price": spaces.Box(low=np.float32(0), high=np.float32(self.max_stock_price),
                                    shape=(self.max_company_number,), dtype=np.float32),
                "end_batch": spaces.Discrete(2)
            }
        )

        self.observation_space = spaces.Dict(
            {
                "indexes": spaces.Dict({
                    'open': spaces.Box(low=np.float32(0), high=np.float32(self.number_infinite),
                                       dtype=np.float32),
                    'close': spaces.Box(low=np.float32(0), high=np.float32(self.number_infinite),
                                        dtype=np.float32),
                    'high': spaces.Box(low=np.float32(0), high=np.float32(self.number_infinite),
                                       dtype=np.float32),
                    'low': spaces.Box(low=np.float32(0), high=np.float32(self.number_infinite),
                                      dtype=np.float32),

                }
                ),
                "day": spaces.Discrete(self.number_infinite),
                "seconds": spaces.Discrete(24 * 3600),
                "company_count": spaces.Discrete(self.max_company_number),
                "prices:": spaces.Dict({
                    "ask_price": spaces.Box(low=np.float32(0), high=np.float32(self.max_stock_price),
                                            shape=(self.max_company_number,), dtype=np.float32),
                    "bid_price": spaces.Box(low=np.float32(0), high=np.float32(self.max_stock_price),
                                            shape=(self.max_company_number,), dtype=np.float32),
                    "price": spaces.Box(low=np.float32(0), high=np.float32(self.max_stock_price),
                                        shape=(self.max_company_number,), dtype=np.float32)}),

                "portfolio_company_count": spaces.Discrete(self.max_company_number),
                "portfolios": spaces.Dict(
                    {
                        "company_id": spaces.MultiDiscrete([self.max_company_number] * self.max_company_number),

                        "volume": spaces.Box(np.float32(0), high=np.float32(self.number_infinite),
                                             shape=(self.max_company_number,),
                                             dtype=np.float32),
                        "buy_price": spaces.Box(low=np.float32(0), high=np.float32(self.max_stock_price),
                                                shape=(self.max_company_number,), dtype=np.float32),
                        "sell_price": spaces.Box(low=np.float32(0), high=np.float32(self.max_stock_price),
                                                 shape=(self.max_company_number,), dtype=np.float32),
                    }),
                "total_value:": spaces.Box(low=np.float32(0), high=np.float32(self.number_infinite), dtype=np.float32),
                "available_fund:": spaces.Box(low=np.float32(0), high=np.float32(self.number_infinite),
                                              dtype=np.float32)

            }
        )

        print(colorize("Initializing data, it may take a couple minutes,please wait...", 'red'))
        db_file = f'{pathlib.Path().absolute()}/asx_gym/db.sqlite3'
        conn = sqlite3.connect(db_file)
        cur = conn.cursor()
        cur.execute("SELECT min(updated_date) as updated_date from stock_dataupdatehistory")
        updated_date = cur.fetchone()
        updated_date = updated_date[0]
        self.max_stock_date = datetime.strptime(updated_date, date_fmt).date()

        if self.user_set_start_date > self.max_stock_date + timedelta(days=-100):
            self.user_set_start_date = self.min_stock_date + timedelta(days=-100)
            self.start_date = self.user_set_start_date

        self.max_transaction_days = (self.max_stock_date - self.min_stock_date).days
        print(colorize(f"Stock date range from {self.min_stock_date} to {self.max_stock_date}", "blue"))
        print(colorize("reading asx index data", 'blue'))
        self.index_df = pd.read_sql_query(
            'SELECT index_date as Date,open_index as Open,close_index as Close,high_index as High,low_index as Low FROM stock_asxindexdailyhistory where index_name="ALL ORD"  order by index_date',
            conn,
            parse_dates={'Date': date_fmt}, index_col=['Date'])

        print(f'Asx index records:\n{self.index_df.count()}')
        print(colorize("reading asx company data", 'blue'))
        self.company_df = pd.read_sql_query('SELECT id,name,description,code,sector_id FROM stock_company', conn)
        print(f'Asx company count:\n{self.company_df.count()}')
        print(colorize("reading asx sector data", 'blue'))
        self.sector_df = pd.read_sql_query('SELECT id,name,full_name FROM stock_sector', conn)
        print(f'Asx sector count:\n{self.sector_df.count()}')
        # print(colorize("reading asx stock data, please wait...", 'blue'))
        # self.price_df = pd.read_sql_query(
        #     f'SELECT * FROM stock_stockpricedailyhistory order by price_date', con,
        #     parse_dates={'price_date': date_fmt})
        # print(f'Asx stock data records:\n{self.price_df.count()}')
        conn.close()
        print(colorize("Data initialized", "green"))
        self.sample_data = np.random.rand(self.display_days)

    def history_indexes(self, days=-1):
        pass

    def history_stock_prices(self, days=-1):
        pass

    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def step(self, action):
        self.ax.clear()
        last_fund = self.history_fund_values[self.step_count]
        self.step_count += 1
        self.draw_stock()
        direction = np.random.randint(100)
        change_price = np.random.randint(1000)

        if direction > 50:
            new_value = last_fund + change_price
        else:
            new_value = last_fund - change_price

        self.history_fund_values.append(new_value)

        done = False
        if self.step_count > 500:
            done = True
        return self.step_count, 0, done, {}

    def draw_stock(self):
        end_date = self.start_date + timedelta(days=self.step_count)
        start_date = self.start_date + timedelta(days=self.step_count - self.display_days)
        stock_index = self.index_df.loc[start_date:end_date]
        display_date = end_date.strftime(date_fmt)

        self.fig, self.ax = mpf.plot(stock_index,
                                     type='candle', mav=(7, 2),
                                     returnfig=True,
                                     title=f'OpenAI ASX Gym - ALL ORD Index {display_date}',
                                     ylabel='Index',

                                     )

        logger.info(f'{len(stock_index)}')
        ax_c = self.ax[0].twinx()

        count = len(stock_index)

        ax_c.plot(self.history_fund_values[self.step_count:self.step_count + count], color='g')
        ax_c.set_ylabel('Total value')

    def reset(self):

        # try to close exist fig if possible
        try:
            plt.close(self.fig)

        except:
            pass

        if not self.keep_same_start_date_when_reset:
            offset_days = self.np_random.randint(0, self.random_start_days)
            self.start_date = self.user_set_start_date + timedelta(days=offset_days)

        logger.info(f'Reset date to {self.start_date}')

        self.step_count = 0
        self.draw_stock()

    def render(self, mode='human'):
        img = get_img_from_fig(self.fig)
        plt.close(self.fig)
        if mode == 'rgb_array':
            return img
        elif mode == 'human':
            from gym.envs.classic_control import rendering
            self.viewer.imshow(img)
            return self.viewer.isopen
