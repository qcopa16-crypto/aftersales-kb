import { get, post, put } from './http'

/** 待审核 FAQ 推荐列表 */
export function fetchFaqRecommendations(params) {
  return get('/settlement/faqs/recommendations', params ?? {})
}

/** 审核推荐 FAQ */
export function reviewFaq(id, action, editedAnswer) {
  return post(`/settlement/faqs/${id}/review`, { action, editedAnswer })
}

/** 已发布 FAQ 列表 */
export function fetchFaqs(params) {
  return get('/settlement/faqs', params ?? {})
}

/** 编辑已发布 FAQ */
export function updateFaq(id, body) {
  return put(`/settlement/faqs/${id}`, body)
}

/** 下线 FAQ */
export function offlineFaq(id) {
  return post(`/settlement/faqs/${id}/offline`)
}
