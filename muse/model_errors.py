"""Safe, actionable model availability errors without exposing provider bodies."""
def model_failure(error):
    status=getattr(error,'status_code',None)
    text=(str(error)+' '+str(getattr(error,'body',''))).lower()
    if status==402 or any(word in text for word in ('insufficient_balance','insufficient balance','insufficient_quota','余额不足')):
        return '账户余额或额度不足，请充值或切换模型后继续。',True
    if status==401:return 'API key 无效或已失效，请检查服务端配置后重试。',True
    if status==403:return '账户或 API key 没有访问权限，请检查供应商权限。',True
    if status==404:return '模型名称或 API 地址不可用，请检查模型配置。',True
    if status in (400,422):return '模型拒绝了请求参数或上下文，请查看模型错误详情并修正适配。',True
    if status==429:return '请求被限流，请稍后重试或切换模型。',False
    if status and status>=500:return '供应商服务暂时异常，请稍后重试或切换模型。',False
    if 'timeout' in type(error).__name__.lower():return '模型请求超时，请检查网络或稍后重试。',False
    if 'connection' in type(error).__name__.lower():return '无法连接模型服务，请检查网络和 API 地址。',False
    return '模型调用失败，请查看模型错误详情。',False
