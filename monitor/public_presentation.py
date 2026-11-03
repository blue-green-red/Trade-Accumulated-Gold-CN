"""Public product wording and opaque model names; stored/private records stay intact."""
import re


def models(settings):
    engines = [m for m in ('codex', 'claude') if m in settings.ai_analysts]
    return dict(zip(('primary', 'secondary'), engines))


def resolve(query, settings):
    mapping = models(settings)
    if 'model' in query and 'analyst' in query: raise ValueError('Conflicting model selectors')
    if 'analyst' in query:  # Existing private previews/bookmarks remain compatible.
        engine = query['analyst'][0]
        if engine not in mapping.values(): raise ValueError('Invalid analyst')
        return engine
    alias = query.get('model', ['primary'])[0]
    if alias not in mapping: raise ValueError('Invalid model')
    return mapping[alias]


def clean_text(text):
    text = re.sub(r'(?<![A-Za-z])(?:codex|claude)(?![A-Za-z])', '金析AI', text, flags=re.I)
    parts = re.split(r'([。！？；;，,])', text)
    result = []
    for i in range(0, len(parts), 2):
        clause = parts[i]
        quality_note = (('采样' in clause or ('京东' in clause and '完整' in clause)) and
                        re.search(r'不完整|不全|并非完整|不是完整|非完整|缺口|缺失|覆盖不足|样本不足|缺少.*完整|不等于完整|不代表完整', clause))
        if not clause.strip() or quality_note: continue
        result.append(clause + (parts[i+1] if i+1 < len(parts) else ''))
    return ''.join(result).strip(' ，,；;')


def present(value, settings):
    aliases = {engine: alias for alias, engine in models(settings).items()}
    def project(item):
        if isinstance(item, dict):
            result = {}
            for key, val in item.items():
                if key == 'confidence': continue
                if key == 'analyst': result['model'] = aliases.get(val, 'primary')
                elif key == 'available_analysts': result['available_models'] = [aliases[m] for m in val if m in aliases]
                else: result[key] = project(val)
            return result
        if isinstance(item, list):
            return [p for val in item if (p := project(val)) not in ('', None)]
        return clean_text(item) if isinstance(item, str) else item
    result = project(value)
    result['update_policy'] = {
        'move_threshold_pct': settings.ai_periodic_min_move_pct if settings.ai_short_review_minutes >= 0 else None,
        'price_basis': '国际现货黄金相对上次分析输入价的涨跌幅',
        'max_idle_minutes': settings.ai_review_max_idle_minutes if settings.ai_short_review_minutes >= 0 else None,
        'scheduled_times': list(settings.ai_schedule_bj),
        'events': ['重要市场消息', '重要经济数据公布'],
        'note': '达到条件后排队更新；交易时段、数据可用性和分析安排会影响完成时间。',
    }
    return result
