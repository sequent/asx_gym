import io
import json
import pathlib
import random
import sqlite3
from datetime import datetime, timedelta

import cv2
import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np
# Data manipulation packages
import pandas as pd
from gym import Env
from gym import spaces, logger
from gym.utils import seeding
from gym.utils.colorize import *

from .asx_image_viewer import AsxImageViewer
from .constants import TOP_UP_FUND, WITHDRAW_FUND, \
    BUY_STOCK, SELL_STOCK, \
    MIN_STOCK_DATE, date_fmt
from .models import StockDailySimulationPrices, StockRecord, AsxAction, AsxObservation
from .utils import create_directory_if_not_exist


class AsxGymEnv(Env):
    metadata = {'render.modes': ['human', 'ansi', 'rgb_array']}

    def __init__(self, **kwargs):

        # random seeds
        self.np_random, seed = seeding.np_random(0)
        seed = seeding.create_seed(32)
        self.seed(seed=seed)

        # figures
        self.fig, self.ax = plt.subplots()
        self.viewer = AsxImageViewer()
        # plot styles
        mc = mpf.make_marketcolors(up='g', down='r',
                                   edge='inherit',
                                   wick={'up': 'blue', 'down': 'orange'},
                                   volume='skyblue',
                                   ohlc='i')
        self.style = mpf.make_mpf_style(base_mpl_style='seaborn-whitegrid', marketcolors=mc)

        self.episode = 0
        self.step_day_count = 0
        self.global_step_count = 0
        self.step_minute_count = 0  # step is 15 min
        self.step_count = 0

        self.transaction_start_time = 10 * 4  # 10:00
        self.transaction_end_time = 16 * 4  # 16:00
        self.min_stock_date = MIN_STOCK_DATE
        self.min_stock_seq = 0

        # default values and configurations
        self.user_set_start_date = kwargs.get('start_date', self.min_stock_date)

        self.user_set_max_simulation_days = kwargs.get('max_days', -1)
        self.start_date = self.user_set_start_date
        self.display_days = kwargs.get('display_days', 20)

        self.keep_same_company_when_reset = kwargs.get('keep_same_company_when_reset', True)
        self.keep_same_start_date_when_reset = kwargs.get('keep_same_start_date_when_reset', False)
        self.simulate_company_number = kwargs.get('simulate_company_number', -1)
        self.simulate_company_list = kwargs.get('simulate_company_list', None)

        self.initial_fund = kwargs.get('initial_fund', 100000)
        self.initial_bank_balance = kwargs.get('initial_bank_balance', 0)

        self.expected_fund_increase_ratio = kwargs.get('expected_fund_increase_ratio', 2.00)
        self.expected_fund_decrease_ratio = kwargs.get('expected_fund_decrease_ratio', 0.20)
        self.transaction_fee_list = kwargs.get('transaction_fee_list', None)

        self.total_value_history_file = None
        self.save_figure = True
        self.save_episode_history = False

        # stock transaction and simulation data
        self.max_transaction_days = 0
        self.need_move_day_forward = False
        self.display_date = ''
        self.available_fund = self.initial_fund
        self.previous_total_fund = self.available_fund
        self.bank_balance = self.initial_bank_balance
        self.portfolios = {}
        self.info = {}
        self.summaries = {
            "episode": 0,
            "steps": 0,

            "state_date": "2020-01-01",
            "end_date": "2020-01-01",
            "indexes": {
                "open": {
                    "date": "2020-01-01",
                    "index": 0,
                },
                "close": {
                    "date": "2020-01-01",
                    "index": 0,
                },
                "high": {
                    "date": "2020-01-01",
                    "index": 0,
                },
                "low": {
                    "date": "2020-01-01",
                    "index": 100000000,
                }
            },
            "values": {
                "open": {
                    "date": "2020-01-01",
                    "value": 0,
                },
                "close": {
                    "date": "2020-01-01",
                    "value": 0,
                },
                "high": {
                    "date": "2020-01-01",
                    "value": 0,
                },
                "low": {
                    "date": "2020-01-01",
                    "value": 100000000,
                }
            },
            "transactions": {
                "buy": {
                    "total": 0,
                    "fulfilled": 0,
                },
                "sell": {
                    "total": 0,
                    "fulfilled": 0,
                },
            }

        }
        self.action = None
        self.directory_name = None
        self.reward = 0
        self.observation = None
        self.total_value = 0
        self.current_display_date_time = ''

        # some constants
        self.max_company_number = 3000
        self.INVALID_COMPANY_ID = 2999
        self.max_stock_price = 100000
        self.number_infinite = 10000000
        self.random_start_days = 100

        self.env_portfolios = {
            "company_id": np.array([self.INVALID_COMPANY_ID] * self.max_company_number),
            "volume": np.array([0.0] * self.max_company_number),
            "buy_price": np.array([0.0] * self.max_company_number),
            "sell_price": np.array([0.0] * self.max_company_number),
            "price": np.array([0.0] * self.max_company_number),
        }
        self.env_prices = {
            "company_id": np.array([self.INVALID_COMPANY_ID] * self.max_company_number),
            "ask_price": np.array([0.0] * self.max_company_number),
            "bid_price": np.array([0.0] * self.max_company_number),
            "price": np.array([0.0] * self.max_company_number),
        }
        self.daily_simulation_prices = {}

        # action and observation spaces
        self._init_spaces()

        # loading data from database
        self._load_stock_data()
        self.seed()

    def history_indexes(self, days=-1):
        pass

    def history_stock_prices(self, days=-1):
        pass

    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def step(self, action):
        self._close_fig()
        self.ax.clear()
        self.info = {}
        display_date = self._get_current_display_date()
        if self.need_move_day_forward:
            self._move_day_forward()
        assert self.action_space.contains(action), "%r (%s) invalid" % (action, type(action))
        end_batch = self._apply_asx_action(action)
        reward = self._calculate_reward()

        self.save_episode_history_data()
        self._draw_stock()

        self.global_step_count += 1
        done = self._is_done()

        if done:
            if self.total_value_history_file:
                self.total_value_history_file.close()
                self.total_value_history_file = None
                return None, 0, True, {}
        else:
            obs = self._get_current_obs()
            self.step_minute_count += 1
            self.step_count += 1
            self.need_move_day_forward = self.step_minute_count > 24 or end_batch

            # update summary
            self.summaries['steps'] = self.step_count

            self.summaries['end_date'] = display_date
            self.summaries['indexes']['close']['index'] = obs['indexes']['close'].item()
            self.summaries['indexes']['close']['date'] = display_date
            high_index = obs['indexes']['high'].item()
            if self.summaries['indexes']['high']['index'] < high_index:
                self.summaries['indexes']['high']['index'] = high_index
                self.summaries['indexes']['high']['date'] = display_date

            low_index = obs['indexes']['low'].item()
            if self.summaries['indexes']['low']['index'] > low_index:
                self.summaries['indexes']['low']['index'] = low_index
                self.summaries['indexes']['low']['date'] = display_date

            return obs, reward, False, self.info

    def reset(self):
        self._close_fig()
        self.episode += 1

        self.step_day_count = 0
        self.step_minute_count = 0
        self.step_count = 0
        self.available_fund = self.initial_fund
        self.previous_total_fund = self.available_fund
        self.bank_balance = self.initial_bank_balance
        self.portfolios = {}
        self.need_move_day_forward = False
        self._init_episode_storage()

        if not self.keep_same_start_date_when_reset:
            offset_days = self.np_random.randint(0, self.random_start_days)
            self.start_date = self.user_set_start_date + timedelta(days=offset_days)

        self._set_start_date()
        display_date = self._get_current_display_date()
        logger.info(f'Reset date to {display_date}')

        if self.simulate_company_list:
            count = len(self.simulate_company_list)
            if (self.simulate_company_number > 0) and (self.simulate_company_number < count):
                company_list = self.simulate_company_list
                random.shuffle(company_list)
                self.simulate_company_list = company_list[:self.simulate_company_number]

        self._generate_daily_simulation_price_for_companies(current_date=display_date)
        self._draw_stock()

        obs = self._get_current_obs()
        # update summary
        self.summaries['episode'] = self.episode
        self.summaries['steps'] = self.step_count
        self.summaries['state_date'] = display_date
        self.summaries['end_date'] = display_date
        self.summaries['indexes']['open']['index'] = obs['indexes']['open'].item()
        self.summaries['indexes']['open']['date'] = display_date
        self.summaries['indexes']['close']['index'] = obs['indexes']['close'].item()
        self.summaries['indexes']['close']['date'] = display_date
        self.summaries['indexes']['high']['index'] = obs['indexes']['high'].item()
        self.summaries['indexes']['high']['date'] = display_date
        self.summaries['indexes']['low']['index'] = obs['indexes']['low'].item()
        self.summaries['indexes']['low']['date'] = display_date
        self.summaries['values']['open']['value'] = self.available_fund
        self.summaries['values']['open']['date'] = display_date
        self.summaries['values']['close']['value'] = self.available_fund
        self.summaries['values']['close']['date'] = display_date
        self.summaries['values']['high']['value'] = self.available_fund
        self.summaries['values']['high']['date'] = display_date
        self.summaries['values']['low']['value'] = self.available_fund
        self.summaries['values']['low']['date'] = display_date

        self.summaries['transactions']['buy']['total'] = 0
        self.summaries['transactions']['buy']['fulfilled'] = 0
        self.summaries['transactions']['sell']['total'] = 0
        self.summaries['transactions']['buy']['fulfilled'] = 0

        return obs

    def render(self, mode='human'):
        if mode == 'ansi':
            pass
        else:
            img = self._get_img_from_fig(self.fig)
            if mode == 'rgb_array':
                return img
            elif mode == 'human':
                self.viewer.imshow(img)
                return self.viewer.is_open

    def _init_episode_storage(self):
        if self.total_value_history_file:
            self.total_value_history_file.close()
        day = datetime.now()
        date_prefix = day.strftime('%Y-%m-%d_%H-%M-%S')
        self.directory_name = f'simulations/{date_prefix}/episode_{str(self.episode).zfill(4)}'
        create_directory_if_not_exist(self.directory_name)
        self.total_value_history_file = open(f'{self.directory_name}/history_values.csv', 'w')

    def _move_day_forward(self):
        self.step_day_count += 1
        self.step_minute_count = 0
        display_date = self.display_date
        if display_date:
            self._generate_daily_simulation_price_for_companies(current_date=display_date)
        self.need_move_day_forward = False

    def _init_spaces(self):
        self.action_space = spaces.Dict(
            {
                "company_count": spaces.Discrete(self.max_company_number),
                "company_id": spaces.MultiDiscrete([self.max_company_number]
                                                   * self.max_company_number),
                "stock_operation": spaces.MultiDiscrete([5]
                                                        * self.max_company_number),
                "volume": spaces.Box(np.float32(0),
                                     high=np.float32(self.number_infinite),
                                     shape=(self.max_company_number,),
                                     dtype=np.float32),
                "price": spaces.Box(low=np.float32(0),
                                    high=np.float32(self.max_stock_price),
                                    shape=(self.max_company_number,),
                                    dtype=np.float32),
                "end_batch": spaces.Discrete(2)
            }
        )
        self.observation_space = spaces.Dict(
            {
                "indexes": spaces.Dict({
                    'open': spaces.Box(low=np.float32(0),
                                       high=np.float32(self.number_infinite),
                                       dtype=np.float32),
                    'close': spaces.Box(low=np.float32(0),
                                        high=np.float32(self.number_infinite),
                                        dtype=np.float32),
                    'high': spaces.Box(low=np.float32(0),
                                       high=np.float32(self.number_infinite),
                                       dtype=np.float32),
                    'low': spaces.Box(low=np.float32(0),
                                      high=np.float32(self.number_infinite),
                                      dtype=np.float32),

                }
                ),
                "day": spaces.Discrete(self.number_infinite),
                "seconds": spaces.Discrete(24 * 3600),
                "company_count": spaces.Discrete(self.max_company_number),
                "prices:": spaces.Dict({
                    "company_id": spaces.MultiDiscrete([self.max_company_number]
                                                       * self.max_company_number),
                    "ask_price": spaces.Box(low=np.float32(0),
                                            high=np.float32(self.max_stock_price),
                                            shape=(self.max_company_number,),
                                            dtype=np.float32),
                    "bid_price": spaces.Box(low=np.float32(0),
                                            high=np.float32(self.max_stock_price),
                                            shape=(self.max_company_number,),
                                            dtype=np.float32),
                    "price": spaces.Box(low=np.float32(0),
                                        high=np.float32(self.max_stock_price),
                                        shape=(self.max_company_number,),
                                        dtype=np.float32)}),

                "portfolio_company_count": spaces.Discrete(self.max_company_number),
                "portfolios": spaces.Dict(
                    {
                        "company_id": spaces.MultiDiscrete([self.max_company_number]
                                                           * self.max_company_number),

                        "volume": spaces.Box(np.float32(0),
                                             high=np.float32(self.number_infinite),
                                             shape=(self.max_company_number,),
                                             dtype=np.float32),
                        "buy_price": spaces.Box(low=np.float32(0),
                                                high=np.float32(self.max_stock_price),
                                                shape=(self.max_company_number,),
                                                dtype=np.float32),
                        "sell_price": spaces.Box(low=np.float32(0),
                                                 high=np.float32(self.max_stock_price),
                                                 shape=(self.max_company_number,),
                                                 dtype=np.float32),
                        "price": spaces.Box(low=np.float32(0),
                                            high=np.float32(self.max_stock_price),
                                            shape=(self.max_company_number,),
                                            dtype=np.float32),
                    }),
                "bank_balance:": spaces.Box(low=np.float32(0),
                                            high=np.float32(self.number_infinite),
                                            dtype=np.float32),

                "total_value:": spaces.Box(low=np.float32(0),
                                           high=np.float32(self.number_infinite),
                                           dtype=np.float32),
                "available_fund:": spaces.Box(low=np.float32(0),
                                              high=np.float32(self.number_infinite),
                                              dtype=np.float32)

            }
        )

    def _buy_stock(self, company_id, price, volume):
        fulfilled = False
        if (volume < 1e-5) and (price > 1e-5):  # buy all available fund
            volume = round(self.available_fund / price, 0)
            if self.available_fund < volume * price:
                volume -= 1

        total_amount = round(volume * price, 3)
        if self.available_fund >= total_amount:
            key = str(company_id)
            stock_record: StockRecord = self.portfolios.get(key, None)
            if stock_record is None:
                stock_record = StockRecord(company_id, volume, price, 0, price)
                self.portfolios[key] = stock_record
            else:
                stock_record.volume += volume
                stock_record.buy_price = price
                stock_record.price = price
            self.available_fund -= total_amount
            fulfilled = True

        # update summary
        self.summaries['transactions']['buy']['total'] += 1
        if fulfilled:
            self.summaries['transactions']['buy']['fulfilled'] += 1
        return fulfilled

    def _sell_stock(self, company_id, price, volume):
        fulfilled = False
        key = str(company_id)
        stock_record: StockRecord = self.portfolios.get(key, None)
        if stock_record is not None:
            if stock_record.volume >= volume:
                total_amount = round(volume * price, 3)
                stock_record.volume -= volume
                stock_record.sell_price = price
                stock_record.price = price
                self.available_fund += total_amount
                fulfilled = True

        # update summary
        self.summaries['transactions']['sell']['total'] += 1
        if fulfilled:
            self.summaries['transactions']['sell']['fulfilled'] += 1
        return fulfilled

    def _is_done(self):
        done = False
        today = self.display_date
        total_value = self.total_value
        min_lost = round(self.initial_fund * self.expected_fund_decrease_ratio, 3)
        max_gain = round(self.initial_fund * self.expected_fund_increase_ratio, 3)
        if (today is None) or (self.step_day_count >= self.max_transaction_days - 1) \
                or (total_value < min_lost) or (total_value > max_gain):
            done = True

        return done

    def _apply_asx_action(self, action):
        fulfilled = False
        self.info["transactions"] = {}
        self.info["companies"] = {}
        company_count = action['company_count']
        end_batch = action['end_batch']
        self.action = action
        for i in range(company_count):
            stock_operation = action['stock_operation'][i]
            company_id = action['company_id'][i]
            price = float(action['price'][i])
            volume = float(action['volume'][i])
            key = str(company_id)

            if (key in self.daily_simulation_data) and \
                    (key in self.daily_simulation_prices):
                company = self.company_df[self.company_df.id == company_id]
                company_name = company.iloc[0, 1]
                company_description = company.iloc[0, 2]
                self.info["companies"][key] = {
                    'name': company_name,
                    'description': company_description
                }
                sector_id = company.iloc[0, 4]
                if sector_id and not np.math.isnan(sector_id):
                    sector_id = int(sector_id)
                    sector = self.sector_df[self.sector_df.id == sector_id]
                    if len(sector) > 0:
                        sector_name = sector.iloc[0, 2]
                        self.info["companies"][key]['sector'] = sector_name

                ask_price = self.daily_simulation_prices[key]['ask_price']
                bid_price = self.daily_simulation_prices[key]['bid_price']
                current_price = self.daily_simulation_prices[key]['price']
                if stock_operation == BUY_STOCK and price >= ask_price:  # buy
                    fulfilled = self._buy_stock(company_id, ask_price, volume)
                    self.info["transactions"][key] = {'action': 'buy',
                                                      'price': bid_price,
                                                      'volume': volume,
                                                      'fulfilled': fulfilled}
                elif stock_operation == SELL_STOCK and price <= bid_price:  # sell
                    fulfilled = self._sell_stock(company_id, bid_price, volume)
                    self.info["transactions"][key] = {'action': 'sell',
                                                      'price': bid_price,
                                                      'volume': volume,
                                                      'fulfilled': fulfilled}
                elif stock_operation == TOP_UP_FUND:
                    pass
                elif stock_operation == WITHDRAW_FUND:
                    pass
                else:
                    self.info["transactions"][key] = {'action': 'hold',
                                                      'price': current_price,
                                                      'volume': -1,
                                                      'fulfilled': fulfilled
                                                      }

        return end_batch

    def _get_current_price_for_company(self, company_id, price):
        key = str(company_id)
        if key in self.daily_simulation_prices:
            return self.daily_simulation_prices[key]['price']
        return price

    def _get_total_value(self):
        total_amount = self.available_fund
        for key, stock_record in self.portfolios.items():
            current_price = self._get_current_price_for_company(key, stock_record.price)
            total_amount += stock_record.volume * current_price

        return round(total_amount, 2)

    def _get_asx_prices(self):
        count = 0
        for key, simulations in self.daily_simulation_data.items():
            self.env_prices['company_id'][count] = simulations.company_id
            prices = simulations.get_next_prices()
            self.env_prices['ask_price'][count] = prices.ask_price
            self.env_prices['bid_price'][count] = prices.bid_price
            self.env_prices['price'][count] = prices.price

            self.daily_simulation_prices[str(simulations.company_id)] = {
                'ask_price': prices.ask_price,
                'bid_price': prices.bid_price,
                'price': prices.price,

            }

            count += 1
        return self.env_prices

    def _get_asx_portfolios(self):
        count = 0
        for key, portfolio in self.portfolios.items():
            self.env_portfolios['company_id'][count] = portfolio.company_id
            self.env_portfolios['volume'][count] = portfolio.volume
            self.env_portfolios['buy_price'][count] = portfolio.buy_price
            self.env_portfolios['sell_price'][count] = portfolio.sell_price
            self.env_portfolios['price'][count] = portfolio.price
            count += 1
        return self.env_portfolios

    def _get_current_display_date(self):
        asx_index = self.index_df.iloc[self.min_stock_seq + self.step_day_count
                                       :self.min_stock_seq
                                        + self.step_day_count + 1].index
        if not asx_index.empty:
            self.display_date = asx_index.astype(str)[0]
        else:
            self.display_date = None
        return self.display_date

    def _close_fig(self):
        # try to close exist fig if possible
        try:
            plt.close(self.fig)
        except:
            pass

    def _set_start_date(self):
        start_date_index = self.start_date.strftime(date_fmt)
        # find first available index data point
        init_seq = self.index_df[self.index_df.index >= start_date_index].iloc[:1, ]
        new_start_date = str(init_seq.index[0])
        self.start_date = datetime.strptime(new_start_date, f'{date_fmt} %H:%M:%S').date()
        self.min_stock_seq = init_seq.Seq[0]

    def save_episode_history_data(self):
        if self.save_episode_history and self.directory_name and self.action and self.observation:
            episode_history_file = open(f'{self.directory_name}/step_{str(self.step_count).zfill(6)}.json', 'w')
            asx_action = AsxAction.from_env_action(self.action)
            asx_observation = AsxObservation(self.observation)
            episode = {
                'date_time': self.current_display_date_time,
                'action': asx_action.to_json_obj(),
                'observation': asx_observation.to_json_obj(),
                'reward': round(self.reward, 2),
                'info': self.info,
                'total_value': self.total_value,
                'summaries': self.summaries
            }
            json.dump(episode, episode_history_file, indent=2)
            # print(episode)

    def _draw_stock(self):
        display_date = self.display_date
        if display_date:
            stock_index = self.index_df.iloc[
                          self.min_stock_seq + self.step_day_count
                          - self.display_days:self.min_stock_seq + self.step_day_count + 1]

            total_minutes = self.step_minute_count * 15
            hour = total_minutes // 60
            minutes = total_minutes - hour * 60
            display_time = f'{hour + 10}:{str(minutes).zfill(2)}'
            total_fund = self.total_value
            self.current_display_date_time = f'{display_date} {display_time}:00'
            if self.total_value_history_file:
                self.total_value_history_file.write(f'{self.current_display_date_time},{total_fund}\n')

            display_title = f'ASX Gym Env Episode:{self.episode} Step:{self.step_count}\n' \
                            f'{display_date} {display_time} Total Value:{total_fund}'
            self.fig, self.axes = mpf.plot(stock_index,
                                           type='candle', mav=(2, 4),
                                           returnfig=True,
                                           volume=True,
                                           title=display_title,
                                           ylabel='Index',
                                           ylabel_lower='Total Value',
                                           style=self.style
                                           )

            ax_c = self.axes[3].twinx()
            changes = stock_index.loc[:, "Change"].to_numpy()
            ax_c.plot(changes, color='navy', marker='o', markeredgecolor='red')
            ax_c.set_ylabel('Value Change')

    def _calculate_reward(self):
        total_fund = self._get_total_value()
        self.total_value = total_fund
        self.index_df.loc[
            self.index_df.Seq == self.min_stock_seq + self.step_day_count, "Volume"] = round(total_fund, 1)
        diff = total_fund - self.previous_total_fund
        self.index_df.loc[
            self.index_df.Seq == self.min_stock_seq + self.step_day_count, "Change"] = round(
            total_fund - self.initial_fund, 1)
        self.previous_total_fund = total_fund
        self.reward = diff

        # update summary
        high_value = self.summaries['values']['high']['value']
        low_value = self.summaries['values']['low']['value']
        display_date = self.display_date
        if high_value < self.total_value:
            self.summaries['values']['high']['value'] = total_fund
            self.summaries['values']['high']['date'] = display_date
        if low_value > self.total_value:
            self.summaries['values']['low']['value'] = total_fund
            self.summaries['values']['low']['date'] = display_date

        self.summaries['values']['close']['value'] = total_fund
        self.summaries['values']['close']['date'] = display_date

        return diff

    def _load_stock_data(self):
        print(colorize("Initializing data, it may take a couple minutes,please wait...", 'red'))
        db_file = f'{pathlib.Path().absolute()}/asx_gym/db.sqlite3'
        conn = sqlite3.connect(db_file)
        cur = conn.cursor()
        cur.execute("SELECT min(updated_date) as updated_date from stock_dataupdatehistory")
        updated_date = cur.fetchone()
        updated_date = updated_date[0]
        cur.execute("SELECT min(index_date) FROM stock_asxindexdailyhistory")
        min_date = cur.fetchone()
        self.min_stock_date = datetime.strptime(min_date[0], date_fmt).date()
        if self.min_stock_date < MIN_STOCK_DATE:
            self.min_stock_date = MIN_STOCK_DATE
        if self.user_set_start_date < self.min_stock_date:
            self.user_set_start_date = self.min_stock_date

        self.max_stock_date = datetime.strptime(updated_date, date_fmt).date()
        if self.user_set_start_date > self.max_stock_date + timedelta(days=-100):
            self.user_set_start_date = self.max_stock_date + timedelta(days=-100)
            self.start_date = self.user_set_start_date
        self.max_transaction_days = (self.max_stock_date - self.min_stock_date).days
        if self.user_set_max_simulation_days > 0:
            self.max_transaction_days = min(self.max_transaction_days,
                                            self.user_set_max_simulation_days)
        print(colorize(f"Stock date range from {self.min_stock_date} to {self.max_stock_date}", "blue"))
        print(colorize("reading asx index data", 'blue'))
        self.index_df = pd.read_sql_query(
            'SELECT 0 as Seq,index_date as Date,open_index as Open,close_index as Close,high_index as High,low_index as Low,1 as Volume,'
            '0 as Change '
            'FROM stock_asxindexdailyhistory where index_name="ALL ORD"  order by index_date',
            conn,
            parse_dates={'Date': date_fmt}, index_col=['Date'])
        self.index_df = self.index_df.reset_index()
        self.index_df.Seq = self.index_df.index
        self.index_df = self.index_df.set_index('Date')
        self.index_df.columns = ['Seq', 'Open', 'Close', 'High', 'Low', 'Volume', 'Change']
        init_seq = self.index_df[self.index_df.index == '2011-01-10']
        self.min_stock_seq = init_seq.Seq[0]
        print(f'Asx index records:\n{self.index_df.count()}')
        print(colorize("reading asx company data", 'blue'))
        self.company_df = pd.read_sql_query('SELECT id,name,description,code,sector_id FROM stock_company', conn)
        print(f'Asx company count:\n{self.company_df.count()}')
        print(colorize("ASX listed companies", 'blue'))
        for index, (cid, name, description, code, sector_id) in self.company_df.iterrows():
            print(f'{colorize(str(cid).rjust(4), "red")}:{colorize(code, "green")}', end="\t")
            if (index + 1) % 5 == 0:
                print('')
        print('')

        print(colorize("reading asx sector data", 'blue'))
        self.sector_df = pd.read_sql_query('SELECT id,name,full_name FROM stock_sector', conn)
        print(f'Asx sector count:\n{self.sector_df.count()}')
        print(colorize("reading asx stock data, please wait...", 'blue'))
        self.price_df = pd.read_sql_query(
            f'SELECT price_date,open_price,close_price,high_price,low_price,company_id FROM stock_stockpricedailyhistory order by price_date',
            conn,
            parse_dates={'price_date': date_fmt}, index_col=['price_date', 'company_id'])
        print(f'Asx stock data records:\n{self.price_df.count()}')
        conn.close()
        print(colorize("reading stock price simulation data", 'blue'))
        daily_simulation_file = f'{pathlib.Path().absolute()}/asx_gym/daily_stock_price.csv'
        self.daily_simulation_df = pd.read_csv(daily_simulation_file)
        self.daily_simulation_df.columns = ['cid', 'day', 'seconds', 'normalized_ask_price', 'normalized_bid_price',
                                            'normalized_stock_price', 'normalized_low_price', 'normalized_high_price']
        self.daily_simulation_df = self.daily_simulation_df.set_index(['cid', 'day'])
        self.min_company_id = 0
        self.max_company_id = max((self.daily_simulation_df.index.get_level_values('cid')))

        self.daily_simulation_data = {}
        self.cached_simulation_records = {}
        print(colorize("Data initialized", "green"))

    @staticmethod
    def normalized_price(high_price, price):
        return round(price / high_price, 3)

    def _get_current_obs(self):
        stock_index = self.index_df.iloc[
                      self.min_stock_seq + self.step_day_count
                      :self.min_stock_seq + self.step_day_count + 1]
        total_value = self.total_value

        obs = {
            "bank_balance": np.array(self.bank_balance),
            "total_value": np.array(total_value),
            "available_fund": np.array(self.available_fund),
            "day": self.step_day_count,
            "second": self.step_minute_count * 15 * 60 + 10 * 3600,
            "company_count": self._get_company_count(),
            "prices": self._get_asx_prices(),
            "indexes": {
                "open": np.array(stock_index.Open[0]),
                "close": np.array(stock_index.Close[0]),
                "high": np.array(stock_index.High[0]),
                "low": np.array(stock_index.Low[0]),
            },
            "portfolio_company_count": len(self.portfolios),
            "portfolios": self._get_asx_portfolios()

        }
        self.observation = obs

        # update summary

        return obs

    def _generate_daily_simulation_price_for_company(self, company_id, open_price, close_price, high_price, low_price):
        simulations = StockDailySimulationPrices(company_id, open_price, close_price, high_price, low_price)
        ratio = self.normalized_price(high_price, low_price)
        key_ration = str(ratio)
        if key_ration in self.cached_simulation_records:
            selected_simulations = self.cached_simulation_records.get(key_ration)
        else:
            selected_simulations = self.daily_simulation_df[self.daily_simulation_df.normalized_low_price == ratio]
            self.cached_simulation_records[key_ration] = selected_simulations
        if len(selected_simulations) > 0:
            numbers = selected_simulations.index.get_level_values('day').unique().to_list()
            random.shuffle(numbers)
            selected_day = numbers[0]
            selected_simulations = selected_simulations.query(f'day=={selected_day}')
            companies = selected_simulations.index.get_level_values('cid').unique().to_list()
            random.shuffle(companies)
            selected_company = companies[0]
            selected_simulation_prices = selected_simulations.query(f'cid=={selected_company}').sort_values('seconds')[
                                         :22]
            prices = []
            for index, (seconds, normalized_ask_price, normalized_bid_price,
                        normalized_stock_price, normalized_low_price, normalized_high_price) \
                    in selected_simulation_prices.iterrows():
                prices.append((normalized_ask_price, normalized_bid_price,
                               normalized_stock_price))

            simulations.init_simulation_prices(prices)
        else:
            simulations.init_simulation_prices([])
        return simulations

    def _get_company_count(self):
        return len(self.daily_simulation_data)

    def _generate_daily_simulation_price_for_companies(self, current_date):
        price_on_current_date_df = self.price_df.query(f'price_date=="{current_date}"')
        self.daily_simulation_data = {}

        self.cached_simulation_records = {}
        for (day, company_id), (open_price, close_price, high_price, low_price) \
                in price_on_current_date_df.iterrows():
            need_simulate = True
            if self.simulate_company_list is not None:
                if company_id not in self.simulate_company_list:
                    need_simulate = False
            if need_simulate:
                company = self.company_df[self.company_df.id == company_id].iloc[0, 1]
                logger.info(
                    f'Generating simulation data for company {colorize(company_id, "blue")}:'
                    f'{colorize(company, "blue")} on {colorize(current_date, "green")}')

                simulations = self._generate_daily_simulation_price_for_company(company_id, open_price, close_price,
                                                                                high_price, low_price)
                if simulations:
                    self.daily_simulation_data[str(simulations.company_id)] = simulations
        logger.info(
            f'Generated simulation data on {colorize(current_date, "green")} '
            f'for {colorize(len(self.daily_simulation_data), "red")} companies')

    def _get_img_from_fig(self, fig, dpi=60):
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi)
        if self.save_figure:
            fig.savefig(f'images/fig_{self.global_step_count}.png', dpi=180)
        buf.seek(0)
        img_arr = np.frombuffer(buf.getvalue(), dtype=np.uint8)
        buf.close()
        img = cv2.imdecode(img_arr, 1)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        return img
