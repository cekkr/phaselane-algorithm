# PCPL Token Trace (generated)

This file is auto-generated. Do not edit by hand.
Regenerate with: `python3 demo/export_token_trace.py --x 4 --token-bits 128 --active-count 1 --seed 1337 --blocks 4`

Parameters:
- x = 4
- cycles = 16
- seed = 1337
- token_bits = 128
- active_count = 1
- a0, b0, c0 = 770266, 779730, 391875

Provider matching order is defined per block by a permutation seeded from the block phase digest. The order is not round-robin and can repeat across block boundaries.

Permutation formula:

$$
\pi_B = \mathrm{PermuteBlock}(perm\_key, B, \Phi_{B \cdot x}, \mathrm{PERMSEED}), \quad idx_t = \pi_B[t \bmod x]
$$

## Block-level permutations

| block B | pi_B (slot order 0..x-1) | matching order |
| --- | --- | --- |
| 0 | [0, 2, 1, 3] | P0 -> P2 -> P1 -> P3 |
| 1 | [2, 1, 0, 3] | P2 -> P1 -> P0 -> P3 |
| 2 | [2, 3, 1, 0] | P2 -> P3 -> P1 -> P0 |
| 3 | [1, 2, 0, 3] | P1 -> P2 -> P0 -> P3 |

## Schedule (device-selected provider per cycle)

| t | block | slot | idx (device routes to) |
| --- | --- | --- | --- |
| 0 | 0 | 0 | 0 |
| 1 | 0 | 1 | 2 |
| 2 | 0 | 2 | 1 |
| 3 | 0 | 3 | 3 |
| 4 | 1 | 0 | 2 |
| 5 | 1 | 1 | 1 |
| 6 | 1 | 2 | 0 |
| 7 | 1 | 3 | 3 |
| 8 | 2 | 0 | 2 |
| 9 | 2 | 1 | 3 |
| 10 | 2 | 2 | 1 |
| 11 | 2 | 3 | 0 |
| 12 | 3 | 0 | 1 |
| 13 | 3 | 1 | 2 |
| 14 | 3 | 2 | 0 |
| 15 | 3 | 3 | 3 |

## Device tokens (verbatim)

| t | device token | matches provider |
| --- | --- | --- |
| 0 | `0xa30497f4481cc925d3d2781715c90c11` | P0 |
| 1 | `0x2a387c5c6417f3698eff01518b3593da` | P2 |
| 2 | `0x2bc18699b56f81632c257ab5ba1d82b2` | P1 |
| 3 | `0x7b4d155d27767aa12517514c4cbf1fcb` | P3 |
| 4 | `0xa3ff87a611ea981f45b94c9119a27076` | P2 |
| 5 | `0x589b117f9404275bc0143088de9b345a` | P1 |
| 6 | `0xb95461b084e46785b79183416092d469` | P0 |
| 7 | `0x72df0d6f34301df730d7c9c08a3973e0` | P3 |
| 8 | `0x36c913ecdf9cae1519137dd3ef622f5d` | P2 |
| 9 | `0xc2a8cb5c607b8f95c43328983235db95` | P3 |
| 10 | `0xca36bf7c8e5eef8ea16466a0fda50695` | P1 |
| 11 | `0x4dd1c47313899bce7d358bf7e95bcc9a` | P0 |
| 12 | `0x39d45bffa2f4883bb31d0a493c121460` | P1 |
| 13 | `0xc06525e7f110ccd5237642cf2ddd16a5` | P2 |
| 14 | `0xd3efd120e4c1f4c3428b16a7f749b17e` | P0 |
| 15 | `0x865362b58dc13fa126f169d48bf44870` | P3 |

## Provider lane P0

| t | P0 token | match |
| --- | --- | --- |
| 0 | `0xa30497f4481cc925d3d2781715c90c11` | match |
| 1 | `0xdddc956d5dcbffa06df5d4b70d0f348c` |  |
| 2 | `0x3819d1c5bca942f47ecb0e9d7606ae95` |  |
| 3 | `0xa59372c7945ba50c4b8f4acebe4dfb44` |  |
| 4 | `0x1c5d27f04963f4316f053a1ecdc74b10` |  |
| 5 | `0x77b7c7af00430b5167600194087d6edc` |  |
| 6 | `0xb95461b084e46785b79183416092d469` | match |
| 7 | `0x6ba41efc9902ed9329ce4cc8652ba8d8` |  |
| 8 | `0xf34c7596ec3164486667243127d6b834` |  |
| 9 | `0xd4aa9f9c6ee8b11ac18559ddbd770254` |  |
| 10 | `0xba095e99403cb68b483fa55d9ec0a431` |  |
| 11 | `0x4dd1c47313899bce7d358bf7e95bcc9a` | match |
| 12 | `0x1935e80284a9bfb42610f2151fe2e2d5` |  |
| 13 | `0x7d922fd17acb1da3d0e931cb9ee41fd2` |  |
| 14 | `0xd3efd120e4c1f4c3428b16a7f749b17e` | match |
| 15 | `0x0c0723fc4ca1826519db7cef6528c996` |  |

## Provider lane P1

| t | P1 token | match |
| --- | --- | --- |
| 0 | `0x2cf89630d7b02df7e47e42443596f56c` |  |
| 1 | `0xb85d28d8ab96a4f046b3169ba179831a` |  |
| 2 | `0x2bc18699b56f81632c257ab5ba1d82b2` | match |
| 3 | `0xd145cd5b1684a9fd86bdae075898a8ba` |  |
| 4 | `0x30fb663ada5c97ecca8215aa59fa0efa` |  |
| 5 | `0x589b117f9404275bc0143088de9b345a` | match |
| 6 | `0x385070e3923fc4846e8e7ddff6e94f46` |  |
| 7 | `0xbce338f02af2efcb85f1df651dc458dd` |  |
| 8 | `0xfb58a0a53fa7f3049a7f3f33dbe0512d` |  |
| 9 | `0xf5529c4414c0663446069c60d4584960` |  |
| 10 | `0xca36bf7c8e5eef8ea16466a0fda50695` | match |
| 11 | `0xa496b675677c1ac61bb5cf82b7e0bcdb` |  |
| 12 | `0x39d45bffa2f4883bb31d0a493c121460` | match |
| 13 | `0xa0d19f3752b96101dd425238cd3bee92` |  |
| 14 | `0xebd5e2a5b70011b9bd8a8f65a2c24645` |  |
| 15 | `0xb8e639c22afc69bba4a4b81696f89771` |  |

## Provider lane P2

| t | P2 token | match |
| --- | --- | --- |
| 0 | `0xe93acc7f7a47a4796f1d2b12753a8a4e` |  |
| 1 | `0x2a387c5c6417f3698eff01518b3593da` | match |
| 2 | `0x7cb00d5574da2d86bb91f8cfbcda676d` |  |
| 3 | `0xb6def19fe4b14db1432957ead29f064a` |  |
| 4 | `0xa3ff87a611ea981f45b94c9119a27076` | match |
| 5 | `0xaca171b6c612b8a27f2008d2b2b506cb` |  |
| 6 | `0x6c5e66e352817bed12f2e5578c88a59b` |  |
| 7 | `0x0ec5dcf3a8f184b63cc90ba375e40d96` |  |
| 8 | `0x36c913ecdf9cae1519137dd3ef622f5d` | match |
| 9 | `0x2f4e4907bf9eb11bca4263f6738846d5` |  |
| 10 | `0x161811354d727a273f39e4d15b244bee` |  |
| 11 | `0xafcbef793a29ba5c52e22693cf313f7c` |  |
| 12 | `0x0a401cd70d61a1801b13efeb9d4f889a` |  |
| 13 | `0xc06525e7f110ccd5237642cf2ddd16a5` | match |
| 14 | `0x37ec028afc3fbec0633cf5d28b9ea112` |  |
| 15 | `0xb7d8b183251dc020807f7e1e9be0ec9d` |  |

## Provider lane P3

| t | P3 token | match |
| --- | --- | --- |
| 0 | `0x323b771c921f2ee5dfcd2f929e1b934f` |  |
| 1 | `0x14a060c08a08b94dbafcf9efa8b59730` |  |
| 2 | `0x4385030b7274fba3db813ea7b5b2fb09` |  |
| 3 | `0x7b4d155d27767aa12517514c4cbf1fcb` | match |
| 4 | `0xd164c1005c7d96f6df5689eec9683898` |  |
| 5 | `0xbb3d24ed2ff07233627f92b053fca92a` |  |
| 6 | `0x6b0346dfd5f511c2676cbad75551170b` |  |
| 7 | `0x72df0d6f34301df730d7c9c08a3973e0` | match |
| 8 | `0x20e828f5c9954f2b01ae261e16d0dc67` |  |
| 9 | `0xc2a8cb5c607b8f95c43328983235db95` | match |
| 10 | `0x65e7ffeaf83115b8a9492103eed7e970` |  |
| 11 | `0xc6469e768e343a309607c66e12d36263` |  |
| 12 | `0x37f9635fff34e8d9ac571cfa0dec3368` |  |
| 13 | `0x643900fa5d7f91c1059608345edbd663` |  |
| 14 | `0x1cbdf3bdb4f012a048050858bd7a3ae5` |  |
| 15 | `0x865362b58dc13fa126f169d48bf44870` | match |

