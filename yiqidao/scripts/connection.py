"""按逐段时刻识别换乘风险；阈值是产品初筛规则，不是承运人保证。"""

def connection_notes(option):
    if option is None or len(option.segments) < 2:
        return []
    notes = []
    for left, right in zip(option.segments, option.segments[1:]):
        station = left.arr_station or '站点未知'
        same = bool(left.arr_station and right.dep_station and left.arr_station == right.dep_station)
        minimum = 30 if option.mode == 'train' else 120
        if not left.arr_time or not right.dep_time:
            notes.append('换乘时刻缺失，无法核实衔接')
            continue
        minutes = (right.dep_time - left.arr_time).total_seconds() / 60
        if minutes <= 0:
            notes.append(f'{station}换乘间隔{minutes:g}分钟，时刻衔接无效')
        elif not same:
            notes.append(f'{station}→{right.dep_station or "站点未知"}换乘{minutes:g}分钟，跨站或站点信息缺失，接驳未核实')
        elif minutes < minimum:
            notes.append(f'{station}换乘仅{minutes:g}分钟，低于初筛预留{minimum}分钟，实际衔接待确认')
    return notes


def member_connection_risk(member):
    return int(bool(connection_notes(member.outbound) or connection_notes(member.ret)))
