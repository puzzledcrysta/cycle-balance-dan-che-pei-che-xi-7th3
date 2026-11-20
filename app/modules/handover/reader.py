from app.errors import DataError
from app.modules.intake.reader import decode, records
from app.evidence import finding

MISSING = {'', '-', 'n/a', 'null', '未知'}


def read_table(payload, required, allow_empty=False):
    """读取单表头 CSV，返回表头与逐行字典；表头/宽度问题整批拒绝，坏行交给上层隔离。"""
    text, encoding = decode(payload)
    if not text.strip():
        raise DataError('EMPTY', '文件无内容')
    header, rows, bad = None, [], []
    for line, raw in records(text):
        values = [part.strip().removeprefix('﻿') for part in raw]
        if header is None:
            if not any(values):
                continue
            header = values
            if not all(header) or len(set(header)) != len(header):
                raise DataError('HEADER', '表头存在空列或重复字段')
            missing = set(required) - set(header)
            if missing:
                raise DataError('CONTRACT', f'必需字段缺失：{"、".join(sorted(missing))}')
            continue
        if not any(values):
            continue
        if values == header:
            continue
        if len(values) != len(header):
            bad.append(finding({'line': line, 'raw': raw}, 'WIDTH',
                               f'{len(values)} 列，应为 {len(header)} 列'))
            continue
        rows.append({'line': line, 'raw': raw, 'values': dict(zip(header, values))})
    if header is None:
        raise DataError('BLANK_FILE', '文件只有空白')
    if not rows and not bad and not allow_empty:
        raise DataError('HEADER_ONLY', '文件只有表头')
    return header, rows, bad, encoding


def is_missing(value):
    return value.casefold() in MISSING
