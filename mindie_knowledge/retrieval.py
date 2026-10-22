"""[Role] Technical identifier and CJK token boundaries for local BM25.

[Invariants] Retain qualified identifiers, versions and searchable components.
Frequency aggregation must equal individual token expansion; relevance is not
factual confidence. [Timeouts & Locks] Pure CPU work; no waits or locks.
[Side-Effect Policy] No I/O; only the bounded identifier-alias cache is shared.
"""
from __future__ import annotations

from collections import Counter
import re
from functools import lru_cache

_WORDS = re.compile(r"[a-z0-9_]+(?:[./+:-][a-z0-9_]+)*|[\u3400-\u9fff]+", re.I)
_CJK = re.compile(r"^[\u3400-\u9fff]+$")
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$", re.I)
_PARTS = re.compile(r"[./+:-]")
_DIGIT_ALPHA = re.compile(r"[0-9][a-z]")
# Split camelCase/PascalCase/acronym transitions (e.g., DataCopyPad, ACLGraph,
# aclrtSynchronizeStream) and chip/API digit boundaries (e.g., Ascend910B4 ->
# ascend, 910b4) while keeping the full compound token indexed for exact boosts.
_SUBWORDS = re.compile(
    r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+[A-Za-z][A-Za-z0-9]*"
)
_ASCEND_COMPOUND_SPLITS: dict[str, tuple[str, ...]] = {
    "aclgraph": ("acl", "graph"),
    "npugraph": ("npu", "graph"),
    "npugraph_ex": ("npugraph", "npu", "graph", "ex"),
    "torchair": ("torch", "air"),
    "modelslim": ("model", "slim"),
    "msmodelslim": ("ms", "modelslim", "model", "slim"),
    "dspark": ("ds", "spark"),
    "dflash": ("df", "flash"),
    "xlite": ("xl", "lite"),
    "eplb": ("ep", "lb"),
    "mrv2": ("mr", "v2"),
    "hixl": ("hi", "xl"),
    "lmcache": ("lm", "cache"),
    "mooncake": ("moon", "cake"),
    "sfa_v1": ("sfa", "v1"),
    "sfav1": ("sfa", "v1"),
    "dsa_v1": ("dsa", "v1"),
    "dsav1": ("dsa", "v1"),
    "mla_v1": ("mla", "v1"),
    "mlav1": ("mla", "v1"),
    "fa3": ("fa",),
    "fa3_v1": ("fa3", "fa", "v1"),
    "fa3v1": ("fa3", "fa", "v1"),
    "qli_v2": ("qli", "v2"),
    "qliv2": ("qli", "v2"),
    "quarot": ("qua", "rot"),
    "teacache": ("tea", "cache"),
    "memscope": ("mem", "scope"),
    "msmemscope": ("ms", "memscope", "mem", "scope"),
    "msprof": ("ms", "prof"),
    "msprobe": ("ms", "probe"),
    "serving_cast": ("serving", "cast"),
    "servingcast": ("serving", "cast"),
    "tensor_cast": ("tensor", "cast"),
    "tensorcast": ("tensor", "cast"),
}
_ASCEND_COMPOUND_RE = re.compile(
    r"(?:"
    + "|".join(sorted((re.escape(k) for k in _ASCEND_COMPOUND_SPLITS), key=len, reverse=True))
    + r")",
    re.I,
)


def _aliases(raw: str) -> tuple[str, ...]:
    if (
        raw.isascii()
        and raw.islower()
        and raw.isalnum()
        and not _DIGIT_ALPHA.search(raw)
        and not _ASCEND_COMPOUND_RE.search(raw)
    ):
        return ()
    word = raw.casefold()
    aliases: list[str] = []
    for part in _PARTS.split(raw):
        if part.isidentifier() if part.isascii() else _IDENTIFIER.fullmatch(part):
            part_lower = part.casefold()
            aliases.append(part_lower)
            if part_lower in _ASCEND_COMPOUND_SPLITS:
                aliases.extend(_ASCEND_COMPOUND_SPLITS[part_lower])
            for chunk in part.split("_"):
                if not chunk:
                    continue
                lowered = chunk.casefold()
                aliases.append(lowered)
                if lowered in _ASCEND_COMPOUND_SPLITS:
                    aliases.extend(_ASCEND_COMPOUND_SPLITS[lowered])
                if not chunk.islower() or _DIGIT_ALPHA.search(lowered):
                    for piece in _SUBWORDS.findall(chunk):
                        piece_lower = piece.casefold()
                        aliases.append(piece_lower)
                        if piece_lower in _ASCEND_COMPOUND_SPLITS:
                            aliases.extend(_ASCEND_COMPOUND_SPLITS[piece_lower])
    return tuple(dict.fromkeys(part for part in aliases if len(part) > 1 and part != word))


_cached_aliases = lru_cache(maxsize=2048)(_aliases)


def _tokens(text):
    for match in _WORDS.finditer(text):
        raw = match.group()
        word = raw.casefold()
        if _CJK.fullmatch(word) and len(word) > 1:
            for index in range(len(word) - 1):
                yield word[index:index + 2]
        else:
            yield word
            # Long identifiers remain searchable in full, but do not occupy
            # the small cache shared by ordinary repeated software names.
            yield from (_cached_aliases(raw) if len(raw) <= 256 else _aliases(raw))

def tokens(text: str) -> list[str]:
    """Keep exact identifiers and their components searchable, without
    turning software versions into common numeric aliases."""
    return list(_tokens(text))


def token_counts(text: str) -> Counter[str]:
    """Expand each distinct lexeme once when a BM25 index needs frequencies."""
    counts = {}
    for raw, frequency in Counter(_WORDS.findall(text)).items():
        word = raw.casefold()
        # _WORDS emits an entire CJK run separately from identifier lexemes.
        if '\u3400' <= word[0] <= '\u9fff' and len(word) > 1:
            for index in range(len(word) - 1):
                token = word[index:index + 2]
                counts[token] = counts.get(token, 0) + frequency
        else:
            counts[word] = counts.get(word, 0) + frequency
            for alias in (_cached_aliases(raw) if len(raw) <= 256 else _aliases(raw)):
                counts[alias] = counts.get(alias, 0) + frequency
    return Counter(counts)


# Conversational Chinese filler bigrams stripped at query time so incidental
# CJK words inside an otherwise English unit do not skew BM25 IDF weights.
_CJK_STOP_BIGRAMS: frozenset[str] = frozenset({
    "怎么", "如何", "为什", "什么", "应该", "可以", "请问", "一下",
    "问题", "遇到", "时候", "进行", "使用", "我们", "这个", "那个",
    "需要", "出现", "或者", "还是", "以及", "如果", "排查", "解决",
    "定位", "处理", "分析", "修复", "配置", "开启", "关闭", "优化",
    "实现", "跑大", "大模", "模型", "算子", "报错", "不对", "失败",
    "么排", "么办", "么修", "么调", "么解", "么配", "么定", "怎么",
    "为什么", "什", "在昇", "昇腾", "腾上", "上跑",
})

# Compositional Chinese-to-English technical concept bridge (compounds + 2-char
# technical roots) across all 7 MindIE knowledge domains (vllm-ascend, operators,
# infer-core, infer-distributed, infer-observability, infer-ops-compat,
# agent-engineering).
_CJK_TECH_ALIASES: dict[str, tuple[str, ...]] = {
    # 1. Position Embeddings & Attention Variants
    "旋转位置编码": ("rope", "rotary", "interleaved_rope_npu_op"),
    "旋转编码": ("rope", "rotary", "interleaved_rope_npu_op"),
    "位置编码": ("rope", "rotary"),
    "多维旋转": ("mrope", "triton_mrope", "npu_mrope"),
    "线性注意力": ("linear", "gdn", "deltanet", "fla"),
    "门控增量": ("gdn", "deltanet", "chunk_gated_delta_rule"),
    "潜在注意力": ("mla", "latent"),
    "稀疏注意力": ("sparse", "sfa", "nsa", "sparse_flash_attention"),
    "块稀疏": ("block_sparse", "qwen_sparse_attn"),
    "闪电索引": ("lightning", "indexer", "qli"),
    "索引器": ("indexer", "lightning"),
    "融合注意力": ("fia", "fusedinferattentionscore", "flash_mla"),
    "分组查询": ("gqa", "fia"),
    "分页注意力": ("paged", "tnd"),
    "分页kv": ("paged", "kvcache"),
    "分页缓存": ("paged", "block_table"),
    "滑动窗口": ("swa", "sliding", "window"),
    # 2. Speculative Decoding & Draft Proposers
    "投机采样": ("speculative", "proposer", "eagle", "mtp"),
    "投机解码": ("speculative", "proposer", "eagle", "mtp"),
    "投机推理": ("speculative", "proposer", "mtp"),
    "投机": ("speculative", "proposer", "mtp", "eagle"),
    "草稿模型": ("draft", "proposer", "eagle", "eagle3"),
    "草稿头": ("draft", "mtp", "eagle"),
    "草稿": ("draft", "proposer"),
    "树状验证": ("eagle", "medusa", "proposer"),
    "拒绝采样": ("spec_decode_entropy_verify", "posterior_threshold"),
    "接受率": ("acceptance", "speculative", "mtp"),
    "多词预测": ("mtp", "num_nextn_predict_layers"),
    "后缀解码": ("suffix", "ngram", "proposer"),
    "并行扩散": ("dflash", "dspark", "proposer"),
    # 3. Graph Capture, Compilation & Runtime Modes
    "图模式": ("aclgraph", "torchair", "graph"),
    "静态图": ("aclgraph", "torchair", "npugraph_ex"),
    "图捕获": ("aclgraph", "capture"),
    "捕获失效": ("aclgraph", "capture", "invalidation"),
    "捕获": ("capture", "aclgraph"),
    "重放失败": ("replay", "aclgraph", "npugraph_ex"),
    "断图": ("graph", "break", "dynamo", "recompilation"),
    "分桶": ("bucket", "piecewise", "full_decode_only", "padded"),
    "分段捕获": ("piecewise", "aclgraph"),
    "全图捕获": ("full_decode_only", "aclgraph"),
    "重复编译": ("recompilation", "do_not_specialize"),
    "重编译": ("recompilation", "do_not_specialize", "dynamo"),
    "特化": ("specialization", "do_not_specialize", "constexpr"),
    "动态形状": ("dynamic", "shape"),
    "图融合": ("npugraph_ex", "graphfusionpassmanager", "patternfusionbasepass"),
    "自动融合": ("autofuse", "inductor", "loopir"),
    "超核": ("superkernel", "super_kernel", "icache"),
    "二进制融合": ("superkernel", "super_kernel"),
    "模型转换": ("atc", "onnx", "om"),
    "离线模型": ("atc", "onnx", "om"),
    # 4. MoE, Expert Parallelism, Load Balancing & Collective Communication
    "专家并行": ("ep", "moe", "expert"),
    "混合专家": ("moe", "expert"),
    "负载均衡": ("eplb", "balancer", "dyntra_lb", "flashlb"),
    "热点路由": ("eplb", "balancer", "dyntra_lb"),
    "动态路由": ("eplb", "dyntra_lb", "routing"),
    "冗余专家": ("eplb", "redundant", "expert"),
    "共享专家": ("moe_shared_expert_decode_dual_stream", "shared"),
    "双流": ("dual_stream", "multi_stream", "dbo"),
    "多流": ("multi_stream", "npu_stream_switch", "dbo"),
    "预取": ("npu_prefetch", "prefetch", "l2"),
    "通算融合": ("mc2", "matmulallreduce", "matmulreducescatter", "allgathermatmul"),
    "矩阵乘": ("matmul", "gemm", "mmad"),
    "分组矩阵乘": ("grouped_matmul", "groupedmatmul", "gmm", "aclnngroupedmatmulswiglu"),
    "激活融合": ("swiglu", "groupedmatmulswiglu", "aclnngroupedmatmulswiglu"),
    "零token": ("zero", "split_item", "groupedmatmul"),
    "集合通信": ("hccl", "allreduce", "collective"),
    "通信超时": ("hccl", "timeout", "ei0006", "hccl_timeout_secondary"),
    "集合通信超时": ("hccl", "timeout", "ei0006", "hccl_timeout_secondary"),
    "次生超时": ("hccl_timeout_secondary", "secondary", "timeout"),
    "通信域": ("hccl", "comm", "ranktable", "buffsize"),
    "网卡绑定": ("hccl_if_ip", "hccl_socket_ifname", "bond0"),
    "链路状态": ("hccn_tool", "rocev2", "link"),
    "链路闪断": ("hccn", "roce", "ei0006", "link"),
    "对称内存": ("shmem", "symmetric", "rma", "sdma"),
    "单边通信": ("shmem", "rma", "put", "signal"),
    "空闲rank": ("idle", "dp", "rank", "collective"),
    # 5. PD Disaggregation, KV Cache, Prefix Caching & Context Parallelism
    "预填充": ("prefill", "ttft"),
    "解码分离": ("pd", "disaggregation", "mooncake"),
    "分离部署": ("pd", "disaggregation", "kv_transfer_config"),
    "三阶段分离": ("epd", "disaggregation", "mooncake"),
    "队列反压": ("backpressure", "dynamo", "router", "529"),
    "乱码": ("gibberish", "repeated_ngram", "punctuation_run", "pd"),
    "重复词": ("repeated_ngram", "gibberish"),
    "绑核": ("cpu_binding", "bind_cpus", "numa"),
    "中断隔离": ("irq", "isolation", "numa"),
    "亲和性": ("affinity", "numa", "bind_cpus"),
    "上下文并行": ("context_parallel", "pcp_size", "dcp_size", "ulysses", "ring"),
    "长序列": ("context_parallel", "ulysses", "ring", "zigzag_cp_short_seq"),
    "序列切分": ("kv", "partition", "pcp_size", "dcp_size"),
    "在线softmax": ("online_softmax", "lse", "cp_dcp_online_softmax_merge"),
    "前缀缓存": ("prefix", "ucm", "lmcache", "ucmconnector"),
    "前缀路由": ("radix", "prefix", "dynamo", "router"),
    "缓存池": ("kv_pool", "layerwise_kv_pool", "ascendstoreconnector"),
    "跨机共享": ("distributed", "prefix", "sharing", "ucm", "lmcache"),
    "卸载": ("offload", "recompute", "swap", "kvcache"),
    "重计算": ("recompute", "recomputecpuoffloadconnector", "recomputescheduler"),
    "分块预填充": ("chunked", "prefill", "cla"),
    "睡眠模式": ("sleep_mode", "enable_sleep_mode", "netloader"),
    "首字时延": ("ttft", "prefill", "bottleneck"),
    "首字延迟": ("ttft", "prefill", "bottleneck"),
    "吐字时延": ("tpot", "itl", "decode", "bottleneck"),
    "吐字延迟": ("tpot", "itl", "decode", "bottleneck"),
    "视觉编码器": ("mm_encoder_attention", "ascendmmencoderattention", "vit"),
    "多模态": ("multimodal", "vlm", "vit", "mm_encoder_attention"),
    "视频生成": ("dit", "hunyuan", "teacache", "ulysses"),
    "具身智能": ("embodied", "vla", "openvla", "pi0"),
    # 6. Quantization, Precision & Observability / Debugging
    "权重量化": ("quant", "modelslim", "w8a8", "w4a8", "mxfp8"),
    "动态量化": ("dynamic_quant", "mxfp8", "w8a8"),
    "反量化": ("dequant", "antiquant"),
    "旋转量化": ("quarot", "hadamard", "flatquant"),
    "异常值": ("outlier", "clip", "precision_low_bit_quant_clip"),
    "精度比对": ("msprobe", "precisiondebugger", "cosine_similarity"),
    "逐层精度": ("msprobe", "precisiondebugger", "layerwise"),
    "余弦相似度": ("cosine_similarity", "msprobe"),
    "精度不对": ("precision", "bisect", "accuracy"),
    "精度异常": ("precision", "divergence", "nan"),
    "精度发散": ("precision", "divergence", "accumulation"),
    "累加精度": ("accumulation", "fp32", "precision"),
    "确定性": ("deterministic", "batch_invariant", "hccl_deterministic"),
    "尾块": ("tail", "padding", "alignment"),
    "舍入模式": ("round_mode_cast_rint", "rounding", "cast"),
    "舍入": ("rounding", "round_mode_cast_rint"),
    "维度不匹配": ("rope", "shape", "mismatch"),
    "快慢卡": ("cluster", "slow", "fast", "rank", "msprof"),
    "慢卡": ("slow", "rank", "msprof", "free_time"),
    "假快卡": ("pseudo", "fast", "rank", "msprof"),
    "黑匣子": ("msnpureport", "blackbox", "msaicerr"),
    "异常栈": ("msaicerr", "coredump", "gdb", "plog"),
    "内存踩踏": ("msmemscope", "corruption", "507015", "dirty"),
    "吞吐建模": ("msmodeling", "serving_cast", "tensor_cast"),
    "容量规划": ("msmodeling", "serving_cast", "tensor_cast"),
    "周期预测": ("mskpp", "cycle", "prediction"),
    # 7. Hardware, Memory, AscendC & Triton Operator Optimization
    "显存溢出": ("oom", "507001", "hccl_buffsize"),
    "爆显存": ("oom", "507001", "peak", "memory"),
    "显存碎片": ("fragmentation", "max_split_size_mb", "507001", "pytorch_npu_alloc_conf"),
    "内存分配失败": ("507001", "acl_error_rt_memory_allocation", "fragmentation"),
    "显存泄漏": ("memory", "leak", "msmemscope", "oom"),
    "内存复用": ("imas", "devmm", "reuse"),
    "非对齐搬运": ("datacopypad", "datacopypad_elimination", "alignment"),
    "非对齐": ("datacopypad", "alignment"),
    "冲突": ("conflict", "ub_bank_conflict"),
    "字节对齐": ("alignment", "gm_512b_alignment"),
    "对齐": ("alignment", "datacopypad"),
    "核间同步": ("sync", "setflag", "waitflag", "pipebarrier"),
    "同步屏障": ("pipebarrier", "setflag", "waitflag", "sync"),
    "死锁": ("deadlock", "sync", "hazard"),
    "流水线踩踏": ("hazard", "pipe", "sync"),
    "流水冲突": ("hazard", "pipe", "sync"),
    "双缓冲": ("double", "buffer", "pingpong", "multibuffer"),
    "乒乓": ("pingpong", "double", "buffer", "l1", "l0"),
    "非法地址": ("507015", "507015_illegal_address", "illegal"),
    "内存越界": ("507015", "507035", "overflow", "illegal"),
    "越界": ("overflow", "507015", "illegal"),
    "算子卡死": ("hang", "507015", "aicore"),
    "卡死": ("hang", "timeout", "507015"),
    "迁移": ("migration", "gpu"),
    "核函数": ("kernel",),
    "搬运受限": ("mte2", "mte3", "memory", "bottleneck"),
    "计算受限": ("cube", "vector", "compute", "bottleneck"),
    "流水停顿": ("stall", "pipe", "roofline", "msprof"),
    "性能瓶颈": ("bottleneck", "convergence", "roofline"),
    "天际线": ("roofline", "mfu", "msprof"),
    "流水掩盖": ("pipeline", "overlap", "catlass"),
    "下沉": ("sink", "tiling", "host"),
    "向量归约": ("vector", "reduction", "wholereduce", "blockreduce"),
    "树形归约": ("tree", "reduction", "wholereduce", "blockreduce"),
    "归约": ("reduction", "wholereduce", "blockreduce", "argreduce"),
    "累加": ("accumulation", "accumulator"),
    "毕昇编译器": ("bisheng", "ccec", "bisheng_ccec_compile"),
    "毕昇": ("bisheng", "ccec", "bishengir"),
    "编译报错": ("compile", "compilation", "161001", "ccec"),
    "算子注册": ("opdef", "register_custom_ops", "meta_registration", "161001"),
    "溢出": ("overflow", "ub"),
    "双die": ("dual", "die", "phy", "910c"),
    "物理id": ("phy", "id", "visible_devices"),
    "绑卡": ("visible_devices", "phy", "die"),
    "参数非法": ("161002", "acl_error_invalid_param", "group_list"),
    "容器": ("container", "docker", "davinci_manager", "100001"),
    "设备挂载": ("davinci_manager", "hisi_hdc", "devmm_svm", "100001"),
    "驱动缺失": ("libascend_hal", "driver", "100001"),
    "二分定位": ("bisect", "git", "regression"),
    "核心集": ("coreset", "aisbench", "herding"),
    "租约": ("lease", "isolation", "container"),
}

# Reverse English-to-CJK bigram index so English queries also match local
# drafts or graded units authored in Chinese.
_ENG_TO_CJK_BIGRAMS: dict[str, tuple[str, ...]] = {}
for _cjk_phrase, _eng_tuple in _CJK_TECH_ALIASES.items():
    _cjk_only = "".join(ch for ch in _cjk_phrase if "\u3400" <= ch <= "\u9fff")
    if len(_cjk_only) >= 2:
        _bigrams = tuple(_cjk_only[i : i + 2] for i in range(len(_cjk_only) - 1))
        for _eng in _eng_tuple:
            _prev = _ENG_TO_CJK_BIGRAMS.get(_eng, ())
            _ENG_TO_CJK_BIGRAMS[_eng] = tuple(dict.fromkeys([*_prev, *_bigrams]))


def query_tokens(text: str) -> list[str]:
    """Expand natural-language queries with bidirectional CJK-English domain aliases
    while stripping conversational CJK stopword bigrams."""
    raw_tokens = tokens(text)
    filtered = [tok for tok in raw_tokens if tok not in _CJK_STOP_BIGRAMS]
    base = filtered if filtered else raw_tokens
    lowered = text.casefold()
    extra: list[str] = []
    for phrase, eng_terms in _CJK_TECH_ALIASES.items():
        if phrase in lowered:
            for term in eng_terms:
                extra.extend(tokens(term))
    for tok in base:
        if tok in _ENG_TO_CJK_BIGRAMS:
            extra.extend(_ENG_TO_CJK_BIGRAMS[tok])
    return list(dict.fromkeys([*base, *extra]))
