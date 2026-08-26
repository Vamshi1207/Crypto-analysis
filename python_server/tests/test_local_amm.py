import unittest
from decision import local_amm

class TestLocalAMM(unittest.TestCase):
    def test_cpmm_math(self):
        # Configure a dummy pool
        local_amm.configure_pools([{
            "pool_id": "dummy_pool",
            "base_vault": "base_v",
            "quote_vault": "quote_v",
            "base_decimals": 9,
            "quote_decimals": 6,
            "fee_pct": 0.25
        }])
        
        # Set some reserves
        with local_amm._lock:
            local_amm._pools["dummy_pool"]["base_reserve_raw"] = 100 * (10**9) # 100 SOL
            local_amm._pools["dummy_pool"]["quote_reserve_raw"] = 15000 * (10**6) # 15k USDC
            local_amm._pools["dummy_pool"]["ready"] = True
            
        # Quote 1 SOL in
        amount_in = 1 * (10**9)
        # Expected: x * y = k
        # k = 100 * 15000 = 1,500,000
        # fee = 0.25% of 1 SOL = 0.0025 SOL
        # amount_in_after_fee = 0.9975 SOL
        # new_base = 100 + 0.9975 = 100.9975
        # new_quote = k / 100.9975 = 14851.852273...
        # amount_out = 15000 - 14851.852273 = 148.147726 USDC
        
        # With integer math (as implemented):
        # num = 0.9975 * 10^9 * 15000 * 10^6
        # den = 100 * 10^9 + 0.9975 * 10^9
        
        out = local_amm.get_quote("dummy_pool", amount_in, True)
        self.assertIsNotNone(out)
        self.assertTrue(148 * (10**6) <= out <= 149 * (10**6), f"Out was {out}")

if __name__ == '__main__':
    unittest.main()
