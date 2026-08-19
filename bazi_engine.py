#!/usr/bin/env python3
"""
八字排盘核心引擎
基于寿星天文历(sxtwl)进行精确的公历转农历和节气计算
"""

import sxtwl
import datetime
import math

# ==================== 天干地支基础数据 ====================
GAN = ['甲', '乙', '丙', '丁', '戊', '己', '庚', '辛', '壬', '癸']
ZHI = ['子', '丑', '寅', '卯', '辰', '巳', '午', '未', '申', '酉', '戌', '亥']
GAN_WUXING = {'甲': '木', '乙': '木', '丙': '火', '丁': '火', '戊': '土',
              '己': '土', '庚': '金', '辛': '金', '壬': '水', '癸': '水'}
ZHI_WUXING = {'子': '水', '丑': '土', '寅': '木', '卯': '木', '辰': '土',
              '巳': '火', '午': '火', '未': '土', '申': '金', '酉': '金',
              '戌': '土', '亥': '水'}
WUXING = ['金', '木', '水', '火', '土']

# 纳音五行表（60甲子纳音）
NAYIN = {
    '甲子': '海中金', '乙丑': '海中金', '丙寅': '炉中火', '丁卯': '炉中火',
    '戊辰': '大林木', '己巳': '大林木', '庚午': '路旁土', '辛未': '路旁土',
    '壬申': '剑锋金', '癸酉': '剑锋金', '甲戌': '山头火', '乙亥': '山头火',
    '丙子': '涧下水', '丁丑': '涧下水', '戊寅': '城头土', '己卯': '城头土',
    '庚辰': '白蜡金', '辛巳': '白蜡金', '壬午': '杨柳木', '癸未': '杨柳木',
    '甲申': '泉中水', '乙酉': '泉中水', '丙戌': '屋上土', '丁亥': '屋上土',
    '戊子': '霹雳火', '己丑': '霹雳火', '庚寅': '松柏木', '辛卯': '松柏木',
    '壬辰': '长流水', '癸巳': '长流水', '甲午': '砂中金', '乙未': '砂中金',
    '丙申': '山下火', '丁酉': '山下火', '戊戌': '平地木', '己亥': '平地木',
    '庚子': '壁上土', '辛丑': '壁上土', '壬寅': '金箔金', '癸卯': '金箔金',
    '甲辰': '覆灯火', '乙巳': '覆灯火', '丙午': '天河水', '丁未': '天河水',
    '戊申': '大驿土', '己酉': '大驿土', '庚戌': '钗环金', '辛亥': '钗环金',
    '壬子': '桑柘木', '癸丑': '桑柘木', '甲寅': '大溪水', '乙卯': '大溪水',
    '丙辰': '沙中土', '丁巳': '沙中土', '戊午': '天上火', '己未': '天上火',
    '庚申': '石榴木', '辛酉': '石榴木', '壬戌': '大海水', '癸亥': '大海水',
}

# 十神关系
# 以日干为"我"，看其他干支与日干的关系
# 五行相生: 金生水, 水生木, 木生火, 火生土, 土生金
# 五行相克: 金克木, 木克土, 土克水, 水克火, 火克金

SHENG = {'金': '水', '水': '木', '木': '火', '火': '土', '土': '金'}
KE = {'金': '木', '木': '土', '土': '水', '水': '火', '火': '金'}

# 阳干: 甲丙戊庚壬(0,2,4,6,8)  阴干: 乙丁己辛癸(1,3,5,7,9)
YANG_GAN = [0, 2, 4, 6, 8]
YIN_GAN = [1, 3, 5, 7, 9]

# 地支藏干表
ZHI_CANGGAN = {
    '子': ['癸'],
    '丑': ['己', '癸', '辛'],
    '寅': ['甲', '丙', '戊'],
    '卯': ['乙'],
    '辰': ['戊', '乙', '癸'],
    '巳': ['丙', '庚', '戊'],
    '午': ['丁', '己'],
    '未': ['己', '丁', '乙'],
    '申': ['庚', '壬', '戊'],
    '酉': ['辛'],
    '戌': ['戊', '辛', '丁'],
    '亥': ['壬', '甲'],
}

# 地支藏干比例
ZHI_CANGGAN_RATIO = {
    '子': [1.0],
    '丑': [0.6, 0.2, 0.2],
    '寅': [0.6, 0.2, 0.2],
    '卯': [1.0],
    '辰': [0.6, 0.2, 0.2],
    '巳': [0.6, 0.2, 0.2],
    '午': [0.7, 0.3],
    '未': [0.6, 0.2, 0.2],
    '申': [0.6, 0.2, 0.2],
    '酉': [1.0],
    '戌': [0.6, 0.2, 0.2],
    '亥': [0.7, 0.3],
}

# 十二长生
CHANGSHENG = {
    '甲': {'亥': '长生', '子': '沐浴', '丑': '冠带', '寅': '临官', '卯': '帝旺',
            '辰': '衰', '巳': '病', '午': '死', '未': '墓', '申': '绝',
            '酉': '胎', '戌': '养'},
    '乙': {'午': '长生', '巳': '沐浴', '辰': '冠带', '卯': '临官', '寅': '帝旺',
            '丑': '衰', '子': '病', '亥': '死', '戌': '墓', '酉': '绝',
            '申': '胎', '未': '养'},
    '丙': {'寅': '长生', '卯': '沐浴', '辰': '冠带', '巳': '临官', '午': '帝旺',
            '未': '衰', '申': '病', '酉': '死', '戌': '墓', '亥': '绝',
            '子': '胎', '丑': '养'},
    '丁': {'酉': '长生', '申': '沐浴', '未': '冠带', '午': '临官', '巳': '帝旺',
            '辰': '衰', '卯': '病', '寅': '死', '丑': '墓', '子': '绝',
            '亥': '胎', '戌': '养'},
    '戊': {'寅': '长生', '卯': '沐浴', '辰': '冠带', '巳': '临官', '午': '帝旺',
            '未': '衰', '申': '病', '酉': '死', '戌': '墓', '亥': '绝',
            '子': '胎', '丑': '养'},
    '己': {'酉': '长生', '申': '沐浴', '未': '冠带', '午': '临官', '巳': '帝旺',
            '辰': '衰', '卯': '病', '寅': '死', '丑': '墓', '子': '绝',
            '亥': '胎', '戌': '养'},
    '庚': {'巳': '长生', '午': '沐浴', '未': '冠带', '申': '临官', '酉': '帝旺',
            '戌': '衰', '亥': '病', '子': '死', '丑': '墓', '寅': '绝',
            '卯': '胎', '辰': '养'},
    '辛': {'子': '长生', '亥': '沐浴', '戌': '冠带', '酉': '临官', '申': '帝旺',
            '未': '衰', '午': '病', '巳': '死', '辰': '墓', '卯': '绝',
            '寅': '胎', '丑': '养'},
    '壬': {'申': '长生', '酉': '沐浴', '戌': '冠带', '亥': '临官', '子': '帝旺',
            '丑': '衰', '寅': '病', '卯': '死', '辰': '墓', '巳': '绝',
            '午': '胎', '未': '养'},
    '癸': {'卯': '长生', '寅': '沐浴', '丑': '冠带', '子': '临官', '亥': '帝旺',
            '戌': '衰', '酉': '病', '申': '死', '未': '墓', '午': '绝',
            '巳': '胎', '辰': '养'},
}


def get_shishen(day_gan_idx, target_gan_idx):
    """计算十神: 以日干为我，看目标天干与我什么关系"""
    day_wx = GAN_WUXING[GAN[day_gan_idx]]
    tgt_wx = GAN_WUXING[GAN[target_gan_idx]]
    day_is_yang = day_gan_idx in YANG_GAN
    tgt_is_yang = target_gan_idx in YANG_GAN
    same_yin_yang = (day_is_yang == tgt_is_yang)

    if day_wx == tgt_wx:
        return '比肩' if same_yin_yang else '劫财'
    elif SHENG.get(day_wx) == tgt_wx:
        # 我生者: 食神/伤官
        return '食神' if same_yin_yang else '伤官'
    elif KE.get(day_wx) == tgt_wx:
        # 我克者: 正财/偏财（同性=偏财，异性=正财）
        return '偏财' if same_yin_yang else '正财'
    elif SHENG.get(tgt_wx) == day_wx:
        # 生我者: 正印/偏印
        return '正印' if not same_yin_yang else '偏印'
    elif KE.get(tgt_wx) == day_wx:
        # 克我者: 正官/七杀
        return '正官' if not same_yin_yang else '七杀'
    return '?'


def get_shishen_by_name(day_gan, target_gan):
    """按天干名称计算十神"""
    day_idx = GAN.index(day_gan)
    tgt_idx = GAN.index(target_gan)
    return get_shishen(day_idx, tgt_idx)


# ==================== 神煞计算 ====================

def calc_shensha(year_gan, year_zhi, month_gan, month_zhi,
                 day_gan, day_zhi, hour_gan, hour_zhi):
    """计算神煞"""
    shensha = []

    # --- 天乙贵人 ---
    guiren_map = {
        '甲': ['丑', '未'], '戊': ['丑', '未'], '庚': ['丑', '未'],
        '乙': ['子', '申'], '己': ['子', '申'],
        '丙': ['酉', '亥'], '丁': ['酉', '亥'],
        '辛': ['寅', '午'],
        '壬': ['卯', '巳'], '癸': ['卯', '巳'],
    }
    day_guiren = guiren_map.get(day_gan, [])
    all_zhi = [year_zhi, month_zhi, day_zhi, hour_zhi]
    for g in day_guiren:
        if g in all_zhi:
            shensha.append(f'天乙贵人({g})')

    # --- 文昌 ---
    wenchang_map = {
        '甲': '巳', '乙': '午', '丙': '申', '丁': '酉',
        '戊': '申', '己': '酉', '庚': '亥', '辛': '子',
        '壬': '寅', '癸': '卯'
    }
    wc = wenchang_map.get(day_gan, '')
    if wc and wc in all_zhi:
        shensha.append(f'文昌({wc})')

    # --- 桃花 ---
    taohua_map = {
        '寅': '卯', '午': '卯', '戌': '卯',  # 寅午戌桃花在卯
        '申': '酉', '子': '酉', '辰': '酉',  # 申子辰桃花在酉
        '巳': '午', '酉': '午', '丑': '午',  # 巳酉丑桃花在午
        '亥': '子', '卯': '子', '未': '子',  # 亥卯未桃花在子
    }
    # 以年支和日支查桃花
    for base_zhi in [year_zhi, day_zhi]:
        th = taohua_map.get(base_zhi, '')
        if th and th in all_zhi:
            shensha.append(f'桃花({th})')

    # # 驿马
    yima_map = {
        '寅': '申', '午': '申', '戌': '申',
        '申': '寅', '子': '寅', '辰': '寅',
        '巳': '亥', '酉': '亥', '丑': '亥',
        '亥': '巳', '卯': '巳', '未': '巳',
    }
    for base_zhi in [year_zhi, day_zhi]:
        ym = yima_map.get(base_zhi, '')
        if ym and ym in all_zhi:
            shensha.append(f'驿马({ym})')

    # 华盖
    huagai_map = {
        '寅': '戌', '午': '戌', '戌': '戌',
        '申': '辰', '子': '辰', '辰': '辰',
        '巳': '丑', '酉': '丑', '丑': '丑',
        '亥': '未', '卯': '未', '未': '未',
    }
    for base_zhi in [year_zhi, day_zhi]:
        hg = huagai_map.get(base_zhi, '')
        if hg and hg in all_zhi:
            shensha.append(f'华盖({hg})')

    # 金舆
    jinyu_map = {
        '甲': '辰', '乙': '巳', '丙': '未', '丁': '申',
        '戊': '未', '己': '申', '庚': '戌', '辛': '亥',
        '壬': '丑', '癸': '寅'
    }
    jy = jinyu_map.get(day_gan, '')
    if jy and jy in all_zhi:
        shensha.append(f'金舆({jy})')

    # 将星
    jiangxing_map = {
        '寅': '午', '午': '午', '戌': '午',
        '申': '子', '子': '子', '辰': '子',
        '巳': '酉', '酉': '酉', '丑': '酉',
        '亥': '卯', '卯': '卯', '未': '卯',
    }
    for base_zhi in [year_zhi]:
        jx = jiangxing_map.get(base_zhi, '')
        if jx and jx in all_zhi:
            shensha.append(f'将星({jx})')

    # 禄神
    lu_map = {
        '甲': '寅', '乙': '卯', '丙': '巳', '戊': '巳',
        '丁': '午', '己': '午', '庚': '申', '辛': '酉',
        '壬': '亥', '癸': '子'
    }
    lu = lu_map.get(day_gan, '')
    if lu:
        count = all_zhi.count(lu)
        if count > 0:
            shensha.append(f'禄神({lu}×{count})')

    # 天德贵人
    tiande_map = {
        '寅': '丁', '卯': '申', '辰': '壬', '巳': '辛',
        '午': '亥', '未': '甲', '申': '癸', '酉': '寅',
        '戌': '丙', '亥': '乙', '子': '巳', '丑': '庚'
    }
    td = tiande_map.get(month_zhi, '')
    all_gan = [year_gan, month_gan, day_gan, hour_gan]
    if td and td in all_gan:
        shensha.append(f'天德贵人({td})')

    # 月德贵人
    yuede_map = {
        '寅': '丙', '卯': '甲', '辰': '壬', '巳': '庚',
        '午': '丙', '未': '甲', '申': '壬', '酉': '庚',
        '戌': '丙', '亥': '甲', '子': '壬', '丑': '庚'
    }
    yd = yuede_map.get(month_zhi, '')
    if yd and yd in all_gan:
        shensha.append(f'月德贵人({yd})')

    # 羊刃
    yangren_map = {
        '甲': '卯', '乙': '辰', '丙': '午', '戊': '午',
        '丁': '未', '己': '未', '庚': '酉', '辛': '戌',
        '壬': '子', '癸': '丑'
    }
    yr = yangren_map.get(day_gan, '')
    if yr and yr in all_zhi:
        shensha.append(f'羊刃({yr})')

    return shensha


# ==================== 节气时刻精确判断 ====================

def get_jieqi_times(year):
    """
    获取指定年份所有节气的精确时刻
    返回: [(jqIndex, datetime), ...] 按时间排序
    """
    jqlist = sxtwl.getJieQiByYear(year)
    result = []
    for jq in jqlist:
        t = sxtwl.JD2DD(jq.jd)
        dt = datetime.datetime(
            int(t.Y), int(t.M), int(t.D),
            int(t.h), int(t.m), int(t.s)
        )
        result.append((jq.jqIndex, dt))
    result.sort(key=lambda x: x[1])
    return result


def is_before_lichun(dt):
    """
    判断给定datetime是否在当年立春之前
    精确到秒
    """
    year = dt.year
    jqlist = get_jieqi_times(year)
    
    # 找立春(jqIndex=3)
    lichun_dt = None
    for idx, jdt in jqlist:
        if idx == 3:  # 立春
            lichun_dt = jdt
            break
    
    if lichun_dt is None:
        # 找不到立春，用上一年或下一年的
        return False
    
    return dt < lichun_dt


def get_exact_year_month_gz(dt):
    """
    精确到时刻的年柱和月柱计算
    以立春为年界，以节气为月界
    
    返回: (year_gan, year_zhi, month_gan, month_zhi)
    """
    year = dt.year
    
    # 获取当年和上一年的节气
    jq_current = get_jieqi_times(year)
    jq_prev = get_jieqi_times(year - 1)
    
    all_jq = jq_prev + jq_current
    
    # 找到出生时间前后的节气
    # 节气索引中，奇数索引为"节"(月界)，偶数为"气"
    # 月界节气: 立春(3),惊蛰(5),清明(7),立夏(9),芒种(11),小暑(13),立秋(15),白露(17),寒露(19),立冬(21),大雪(23),小寒(1)
    # jqIndex: 0=冬至 1=小寒 2=大寒 3=立春 4=雨水 5=惊蛰 6=春分 7=清明 8=谷雨 9=立夏 10=小满 11=芒种 12=夏至 13=小暑 14=大暑 15=立秋 16=处暑 17=白露 18=秋分 19=寒露 20=霜降 21=立冬 22=小雪 23=大雪
    # 月界(节): jqIndex为奇数: 1(小寒), 3(立春), 5(惊蛰), 7(清明), 9(立夏), 11(芒种), 13(小暑), 15(立秋), 17(白露), 19(寒露), 21(立冬), 23(大雪)
    # 但传统上，月柱以"节"为界：立春→寅月, 惊蛰→卯月, 清明→辰月...
    
    # 月界节气及其对应月支
    JIE_MAP = {
        3: 2,   # 立春→寅
        5: 3,   # 惊蛰→卯
        7: 4,   # 清明→辰
        9: 5,   # 立夏→巳
        11: 6,  # 芒种→午
        13: 7,  # 小暑→未
        15: 8,  # 立秋→申
        17: 9,  # 白露→酉
        19: 10, # 寒露→戌
        21: 11, # 立冬→亥
        23: 0,  # 大雪→子
        1: 1,   # 小寒→丑
    }
    
    # 找到出生时间之前最近的"节"
    current_jie_idx = None
    current_jie_dt = None
    current_month_zhi = None
    
    for jq_idx, jq_dt in all_jq:
        if jq_dt > dt:
            break
        if jq_idx in JIE_MAP:
            current_jie_idx = jq_idx
            current_jie_dt = jq_dt
            current_month_zhi = JIE_MAP[jq_idx]
    
    if current_month_zhi is None:
        # 默认用丑月
        current_month_zhi = 1
    
    # 判断年柱：以立春为界
    # 找到出生时间之前最近的立春
    lichun_dt = None
    lichun_year = None
    for jq_idx, jq_dt in all_jq:
        if jq_dt > dt:
            break
        if jq_idx == 3:  # 立春
            lichun_dt = jq_dt
            lichun_year = jq_dt.year
    
    if lichun_dt is None or dt < lichun_dt:
        # 在立春之前，用上一年
        year_for_gz = year - 1
    else:
        year_for_gz = lichun_year
    
    # 计算年柱
    # 以甲子年(4年)为基准: year_gan = (year - 4) % 10, year_zhi = (year - 4) % 12
    year_gan_idx = (year_for_gz - 4) % 10
    year_zhi_idx = (year_for_gz - 4) % 12
    
    # 计算月柱
    # 五虎遁: 年干定月干
    # 甲己年: 正月丙寅  乙庚年: 正月戊寅  丙辛年: 正月庚寅  丁壬年: 正月壬寅  戊癸年: 正月甲寅
    # 起月干 = (year_gan_idx % 5) * 2 + 2
    # 甲己→丙起(2), 乙庚→戊起(4), 丙辛→庚起(6), 丁壬→壬起(8), 戊癸→甲起(0→10%10=0)
    # 月干 = (起月干 + 月序号) % 10, 月序号: 寅=0, 卯=1, ..., 子=10, 丑=11
    start_month_gan = (year_gan_idx % 5) * 2 + 2  # 2,4,6,8,10
    month_offset = (current_month_zhi - 2 + 12) % 12  # 寅=0, 卯=1, ..., 子=10, 丑=11
    month_gan_idx = (start_month_gan + month_offset) % 10
    
    return (
        GAN[year_gan_idx], ZHI[year_zhi_idx],
        GAN[month_gan_idx], ZHI[current_month_zhi]
    )


# ==================== 大运计算 ====================

def calc_dayun(year_gan, gender, solar_date):
    """
    计算大运
    阳男阴女顺行，阴男阳女逆行
    起运岁数 = 从出生日到下一个/上一个节气的天数 / 3
    """
    year_gan_idx = GAN.index(year_gan)
    is_yang = year_gan_idx in YANG_GAN  # 阳干

    # 阳男、阴女顺行；阴男、阳女逆行
    forward = (is_yang and gender == 'male') or (not is_yang and gender == 'female')

    # 月柱
    # 需要月柱的地支来推算大运干支
    # 大运从月柱开始，顺行或逆行
    return forward


# ==================== 核心排盘函数 ====================

def paipan(solar_year, solar_month, solar_day, hour, minute, gender):
    """
    八字排盘
    输入: 公历年月日时分，性别
    输出: 完整八字信息
    """
    # 构造datetime
    dt = datetime.datetime(solar_year, solar_month, solar_day, hour, minute)

    # 使用sxtwl获取农历信息
    day = sxtwl.fromSolar(solar_year, solar_month, solar_day)

    lunar_year = day.getLunarYear()
    lunar_month = day.getLunarMonth()
    lunar_day = day.getLunarDay()
    is_leap = day.isLunarLeap()  # 是否闰月

    # ==================== 年柱 + 月柱 ====================
    # 精确到时刻的年柱月柱计算（以立春分年，以节气分月）
    # 注意: sxtwl的getYearGZ(True)用的是春节分年，不是立春，会导致错误
    year_gan, year_zhi, month_gan, month_zhi = get_exact_year_month_gz(dt)

    # ==================== 日柱 ====================
    day_gz = day.getDayGZ()
    day_gan = GAN[day_gz.tg]
    day_zhi = ZHI[day_gz.dz]

    # ==================== 时柱 ====================
    # 五鼠遁日起时: 甲己还加甲, 乙庚丙作初, 丙辛从戊起, 丁壬庚子居, 戊癸何方发, 壬子是真途
    # 时干 = (日干序号/2) * 2 + 时辰序号  -- 更准确地说:
    # 时辰地支: 子=0, 丑=1, ..., 亥=11  对应23-1, 1-3, ..., 21-23
    # 但实际排盘: 子时=0(23点-1点), 丑=1(1-3点), ...
    # 时干公式: day_gan_idx // 2 * 2 然后加 hour_zhi_idx
    # 五鼠遁: 甲己日起甲子时, 乙庚日起丙子时, 丙辛日起戊子时, 丁壬日起庚子时, 戊癸日起壬子时

    # 确定时辰地支
    # 23-1: 子, 1-3: 丑, 3-5: 寅, 5-7: 卯, 7-9: 辰, 9-11: 巳,
    # 11-13: 午, 13-15: 未, 15-17: 申, 17-19: 酉, 19-21: 戌, 21-23: 亥
    if hour == 23:
        hour_zhi_idx = 0  # 子时
    else:
        hour_zhi_idx = (hour + 1) // 2
    hour_zhi = ZHI[hour_zhi_idx]

    # 五鼠遁起时干
    day_gan_idx = GAN.index(day_gan)
    # 甲己日起甲子时: day_gan_idx 0,5 -> 起甲(0)
    # 乙庚日起丙子时: day_gan_idx 1,6 -> 起丙(2)
    # 丙辛日起戊子时: day_gan_idx 2,7 -> 起戊(4)
    # 丁壬日起庚子时: day_gan_idx 3,8 -> 起庚(6)
    # 戊癸日起壬子时: day_gan_idx 4,9 -> 起壬(8)
    start_gan = (day_gan_idx % 5) * 2
    hour_gan_idx = (start_gan + hour_zhi_idx) % 10
    hour_gan = GAN[hour_gan_idx]

    # ==================== 纳音 ====================
    year_nayin = NAYIN.get(year_gan + year_zhi, '')
    month_nayin = NAYIN.get(month_gan + month_zhi, '')
    day_nayin = NAYIN.get(day_gan + day_zhi, '')
    hour_nayin = NAYIN.get(hour_gan + hour_zhi, '')

    # ==================== 十神 ====================
    day_gan_str = day_gan

    year_gan_shishen = get_shishen_by_name(day_gan_str, year_gan)
    year_zhi_main = ZHI_CANGGAN[year_zhi][0]
    year_zhi_shishen = get_shishen_by_name(day_gan_str, year_zhi_main)

    month_gan_shishen = get_shishen_by_name(day_gan_str, month_gan)
    month_zhi_main = ZHI_CANGGAN[month_zhi][0]
    month_zhi_shishen = get_shishen_by_name(day_gan_str, month_zhi_main)

    day_zhi_main = ZHI_CANGGAN[day_zhi][0]
    day_zhi_shishen = get_shishen_by_name(day_gan_str, day_zhi_main)

    hour_gan_shishen = get_shishen_by_name(day_gan_str, hour_gan)
    hour_zhi_main = ZHI_CANGGAN[hour_zhi][0]
    hour_zhi_shishen = get_shishen_by_name(day_gan_str, hour_zhi_main)

    # ==================== 五行统计 ====================
    wx_count = {'金': 0, '木': 0, '水': 0, '火': 0, '土': 0}
    # 天干五行
    for gan in [year_gan, month_gan, day_gan, hour_gan]:
        wx_count[GAN_WUXING[gan]] += 1
    # 地支主气五行
    for zhi in [year_zhi, month_zhi, day_zhi, hour_zhi]:
        main_gan = ZHI_CANGGAN[zhi][0]
        wx_count[GAN_WUXING[main_gan]] += 1

    total = sum(wx_count.values())
    wx_percent = {k: round(v / total * 100) for k, v in wx_count.items()}

    # ==================== 身旺身弱判断 ====================
    day_wx = GAN_WUXING[day_gan]
    # 得令: 月支主气是否生扶日主
    month_zhi_wx = GAN_WUXING[ZHI_CANGGAN[month_zhi][0]]
    de_ling = month_zhi_wx == day_wx or SHENG.get(month_zhi_wx) == day_wx

    # 得地: 地支中有没有日主的禄/旺/生
    lu_zhi_map = {
        '甲': '寅', '乙': '卯', '丙': '巳', '戊': '巳',
        '丁': '午', '己': '午', '庚': '申', '辛': '酉',
        '壬': '亥', '癸': '子'
    }
    day_lu = lu_zhi_map.get(day_gan, '')
    all_zhi = [year_zhi, month_zhi, day_zhi, hour_zhi]
    has_lu = day_lu in all_zhi

    # 得势: 天干有没有比劫
    has_bijie = False
    for gan in [year_gan, month_gan, hour_gan]:
        if GAN_WUXING[gan] == day_wx:
            has_bijie = True
            break

    # 判断身旺身弱
    score = 0
    if de_ling:
        score += 2
    if has_lu:
        score += 2
    if has_bijie:
        score += 1
    shenwang = score >= 3

    # ==================== 神煞 ====================
    shensha = calc_shensha(year_gan, year_zhi, month_gan, month_zhi,
                           day_gan, day_zhi, hour_gan, hour_zhi)

    # ==================== 地支关系 ====================
    zhi_relations = []
    # 六冲
    chong_pairs = [('子', '午'), ('丑', '未'), ('寅', '申'), ('卯', '酉'),
                   ('辰', '戌'), ('巳', '亥')]
    for a, b in chong_pairs:
        if a in all_zhi and b in all_zhi:
            zhi_relations.append(f'{a}{b}冲')

    # 三合
    sanhe_groups = [('申', '子', '辰'), ('亥', '卯', '未'),
                    ('寅', '午', '戌'), ('巳', '酉', '丑')]
    for g in sanhe_groups:
        match = [z for z in g if z in all_zhi]
        if len(match) >= 2:
            zhi_relations.append(f'{"·".join(match)}半合')

    # 六合
    liuhe_pairs = [('子', '丑'), ('寅', '亥'), ('卯', '戌'),
                   ('辰', '酉'), ('巳', '申'), ('午', '未')]
    for a, b in liuhe_pairs:
        if a in all_zhi and b in all_zhi:
            zhi_relations.append(f'{a}{b}合')

    # 自刑
    if all_zhi.count('申') >= 2:
        zhi_relations.append('申申自刑')
    if all_zhi.count('辰') >= 2:
        zhi_relations.append('辰辰自刑')
    if all_zhi.count('午') >= 2:
        zhi_relations.append('午午自刑')
    if all_zhi.count('酉') >= 2:
        zhi_relations.append('酉酉自刑')
    if all_zhi.count('亥') >= 2:
        zhi_relations.append('亥亥自刑')

    # ==================== 大运 ====================
    year_gan_idx = GAN.index(year_gan)
    is_yang_year = year_gan_idx in YANG_GAN
    forward = (is_yang_year and gender == 'male') or (not is_yang_year and gender == 'female')

    # 起运岁数计算: 从出生日到下一个(顺行)或上一个(逆行)节气天数 / 3
    month_gz_str = month_gan + month_zhi
    if forward:
        # 找下一个节气
        search_date = datetime.date(solar_year, solar_month, solar_day)
        for i in range(1, 32):
            check_date = search_date + datetime.timedelta(days=i)
            check_day = sxtwl.fromSolar(check_date.year, check_date.month, check_date.day)
            check_month_gz = check_day.getMonthGZ()
            check_month_str = GAN[check_month_gz.tg] + ZHI[check_month_gz.dz]
            if check_month_str != month_gz_str:
                days_to_jie = i
                break
        else:
            days_to_jie = 15
    else:
        # 找上一个节气
        search_date = datetime.date(solar_year, solar_month, solar_day)
        for i in range(1, 32):
            check_date = search_date - datetime.timedelta(days=i)
            check_day = sxtwl.fromSolar(check_date.year, check_date.month, check_date.day)
            check_month_gz = check_day.getMonthGZ()
            check_month_str = GAN[check_month_gz.tg] + ZHI[check_month_gz.dz]
            if check_month_str != month_gz_str:
                days_to_jie = i
                break
        else:
            days_to_jie = 15

    qiyun_age = round(days_to_jie / 3, 1)
    # 起运岁数取整(传统取法是3天=1岁, 余数1天=4个月, 1时辰=10天, ...)
    # 向上取整，符合传统"足岁"计算
    qiyun_age_int = math.ceil(days_to_jie / 3) if days_to_jie > 0 else 1

    # 大运干支
    # 60甲子表查序号
    JIAZI = [GAN[i % 10] + ZHI[i % 12] for i in range(60)]
    month_gz_idx_full = JIAZI.index(month_gz_str)

    dayun_list = []
    for i in range(1, 9):  # 8步大运
        if forward:
            dz_idx = (month_gz_idx_full + i) % 60
        else:
            dz_idx = (month_gz_idx_full - i) % 60
        dz_gan = GAN[dz_idx % 10]
        dz_zhi = ZHI[dz_idx % 12]
        start_age = qiyun_age_int + (i - 1) * 10
        end_age = start_age + 9
        start_year = solar_year + start_age
        end_year = solar_year + end_age
        dz_shishen_gan = get_shishen_by_name(day_gan_str, dz_gan)
        dz_shishen_zhi = get_shishen_by_name(day_gan_str, ZHI_CANGGAN[dz_zhi][0])
        dayun_list.append({
            'gan': dz_gan, 'zhi': dz_zhi,
            'gan_shishen': dz_shishen_gan,
            'zhi_shishen': dz_shishen_zhi,
            'start_age': start_age, 'end_age': end_age,
            'start_year': start_year, 'end_year': end_year,
            'nayin': NAYIN.get(dz_gan + dz_zhi, '')
        })

    # ==================== 格局判断 ====================
    # 月令本气取格
    month_zhi_main_gan = ZHI_CANGGAN[month_zhi][0]
    month_zhi_main_shishen = get_shishen_by_name(day_gan_str, month_zhi_main_gan)
    geju = f'{month_zhi_main_shishen}格'

    # ==================== 喜用神初步判断 ====================
    # 根据日主五行动态计算，不再hardcoded
    # 五行关系: 生我者=印星, 同我者=比劫, 我生者=食伤, 我克者=财星, 克我者=官杀
    yin_xing = next((wx for wx in WUXING if SHENG.get(wx) == day_wx), '?')    # 生我者
    bijie_xing = day_wx                                                        # 同我者
    shishang_xing = SHENG.get(day_wx, '?')                                     # 我生者
    caixing_xing = KE.get(day_wx, '?')                                         # 我克者
    guansha_xing = next((wx for wx in WUXING if KE.get(wx) == day_wx), '?')    # 克我者

    if shenwang:
        # 身旺: 喜克泄耗(官杀/食伤/财星)，忌生扶(印星/比劫)
        xiyong = [
            f'{guansha_xing}(官杀)',
            f'{caixing_xing}(财星)',
            f'{shishang_xing}(食伤)'
        ]
        jishen = f'{yin_xing}(印星)、{bijie_xing}(比劫)'
    else:
        # 身弱: 喜生扶(印星/比劫)，忌克泄耗
        xiyong = [f'{yin_xing}(印星)、{bijie_xing}(比劫)']
        jishen = f'{guansha_xing}(官杀)、{caixing_xing}(财星)、{shishang_xing}(食伤)'

    # ==================== 组装结果 ====================
    result = {
        'solar_date': f'{solar_year}年{solar_month}月{solar_day}日 {hour:02d}:{minute:02d}',
        'lunar_date': f'{lunar_year}年{"闰" if is_leap else ""}{lunar_month}月{lunar_day}日',
        'gender': gender,
        'four_pillars': {
            'year': {'gan': year_gan, 'zhi': year_zhi,
                     'gan_shishen': year_gan_shishen,
                     'zhi_shishen': year_zhi_shishen,
                     'nayin': year_nayin,
                     'zhi_canggan': ZHI_CANGGAN[year_zhi]},
            'month': {'gan': month_gan, 'zhi': month_zhi,
                      'gan_shishen': month_gan_shishen,
                      'zhi_shishen': month_zhi_shishen,
                      'nayin': month_nayin,
                      'zhi_canggan': ZHI_CANGGAN[month_zhi]},
            'day': {'gan': day_gan, 'zhi': day_zhi,
                    'gan_shishen': '日主',
                    'zhi_shishen': day_zhi_shishen,
                    'nayin': day_nayin,
                    'zhi_canggan': ZHI_CANGGAN[day_zhi]},
            'hour': {'gan': hour_gan, 'zhi': hour_zhi,
                     'gan_shishen': hour_gan_shishen,
                     'zhi_shishen': hour_zhi_shishen,
                     'nayin': hour_nayin,
                     'zhi_canggan': ZHI_CANGGAN[hour_zhi]},
        },
        'day_master': day_gan,
        'day_master_wuxing': day_wx,
        'wuxing_count': wx_count,
        'wuxing_percent': wx_percent,
        'shenwang': '身旺' if shenwang else '身弱',
        'geju': geju,
        'xiyong': xiyong,
        'jishen': jishen,
        'shensha': shensha,
        'zhi_relations': zhi_relations,
        'dayun': dayun_list,
        'qiyun_age': qiyun_age,
        'qiyun_age_int': qiyun_age_int,
        'forward': forward,
    }

    return result


def format_result(result):
    """格式化输出排盘结果"""
    fp = result['four_pillars']
    print('=' * 60)
    print(f"  公历: {result['solar_date']}")
    print(f"  农历: {result['lunar_date']}")
    print(f"  性别: {result['gender']}")
    print(f"  日主: {result['day_master']}({result['day_master_wuxing']})")
    print(f"  格局: {result['geju']}")
    print(f"  身旺身弱: {result['shenwang']}")
    print('=' * 60)
    print(f"  {'':>6}  {'年柱':^10}  {'月柱':^10}  {'日柱':^10}  {'时柱':^10}")
    print(f"  {'天干':>6}  {fp['year']['gan']:^10}  {fp['month']['gan']:^10}  {fp['day']['gan']:^10}  {fp['hour']['gan']:^10}")
    print(f"  {'地支':>6}  {fp['year']['zhi']:^10}  {fp['month']['zhi']:^10}  {fp['day']['zhi']:^10}  {fp['hour']['zhi']:^10}")
    print(f"  {'十神(干)':>6}  {fp['year']['gan_shishen']:^10}  {fp['month']['gan_shishen']:^10}  {'日主':^10}  {fp['hour']['gan_shishen']:^10}")
    print(f"  {'十神(支)':>6}  {fp['year']['zhi_shishen']:^10}  {fp['month']['zhi_shishen']:^10}  {fp['day']['zhi_shishen']:^10}  {fp['hour']['zhi_shishen']:^10}")
    print(f"  {'纳音':>6}  {fp['year']['nayin']:^10}  {fp['month']['nayin']:^10}  {fp['day']['nayin']:^10}  {fp['hour']['nayin']:^10}")
    print(f"  {'藏干':>6}  {''.join(fp['year']['zhi_canggan']):^10}  {''.join(fp['month']['zhi_canggan']):^10}  {''.join(fp['day']['zhi_canggan']):^10}  {''.join(fp['hour']['zhi_canggan']):^10}")
    print('=' * 60)
    print(f"  五行统计: ", end='')
    for wx in WUXING:
        print(f"{wx}={result['wuxing_count'][wx]}({result['wuxing_percent'][wx]}%) ", end='')
    print()
    print(f"  神煞: {', '.join(result['shensha']) if result['shensha'] else '无'}")
    print(f"  地支关系: {', '.join(result['zhi_relations']) if result['zhi_relations'] else '无特殊关系'}")
    print(f"  喜用神: {result['xiyong']}")
    print(f"  忌神: {result['jishen']}")
    print(f"  起运岁数: {result['qiyun_age']}岁")
    print(f"  大运方向: {'顺行' if result['forward'] else '逆行'}")
    print('-' * 60)
    print(f"  大运排盘:")
    for dz in result['dayun']:
        print(f"    {dz['start_age']:>3d}-{dz['end_age']:>3d}岁  {dz['gan']}{dz['zhi']}  "
              f"{dz['gan_shishen']}/{dz['zhi_shishen']}  ({dz['nayin']})")
    print('=' * 60)


if __name__ == '__main__':
    # 测试: 2004年2月11日 08:30 男 广东云浮
    # 预期结果: 甲申 丙寅 庚申 庚辰
    result = paipan(2004, 2, 11, 8, 30, 'male')
    format_result(result)
