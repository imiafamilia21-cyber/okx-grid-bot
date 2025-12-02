class StopVoronPro:
    """
    Stop Voron v5 — промышленный стоп-лосс с защитой от edge cases
    """
    
    def __init__(self, 
                 base_atr_mult=2.0,
                 min_risk_pct=0.005,
                 max_risk_pct=0.04,
                 trailing_enabled=True,
                 use_dynamic_atr=True,
                 exit_mode="intrabar",
                 slippage_pct=0.001,
                 trailing_activation_mult=0.5):
        
        self.base_atr_mult = base_atr_mult
        self.min_risk_pct = min_risk_pct
        self.max_risk_pct = max_risk_pct
        self.trailing_enabled = trailing_enabled
        self.use_dynamic_atr = use_dynamic_atr
        self.exit_mode = exit_mode
        self.slippage_pct = slippage_pct
        self.trailing_activation_mult = trailing_activation_mult

    def calculate_atr_multiplier(self, volatility_ratio, market_regime):
        if not self.use_dynamic_atr:
            return self.base_atr_mult
            
        multiplier = self.base_atr_mult
        if volatility_ratio > 1.5:
            multiplier *= 1.3
        elif volatility_ratio < 0.7:
            multiplier *= 0.8
        if market_regime == "trending":
            multiplier *= 0.9
        elif market_regime == "volatile":
            multiplier *= 1.2
        return round(multiplier, 2)
    
    def calculate_stop(self, entry, atr, side, current_price=None, 
                      volatility_ratio=1.0, market_regime="normal"):
        if entry <= 0 or atr < 0:
            raise ValueError("Некорректные входные данные")
        
        atr_mult = self.calculate_atr_multiplier(volatility_ratio, market_regime)
        
        # БАЗОВЫЙ СТОП
        if side == "long":
            stop = entry - (atr * atr_mult)
            min_stop = entry * (1 - self.min_risk_pct)
            stop = max(stop, min_stop)
        else:
            stop = entry + (atr * atr_mult)
            max_stop = entry * (1 + self.min_risk_pct)
            stop = min(stop, max_stop)
        
        # ТРЕЙЛИНГ
        if self.trailing_enabled and current_price:
            activation = atr * self.trailing_activation_mult
            if side == "long" and (current_price - entry) > activation:
                new_stop = current_price - (atr * atr_mult * 0.7)
                new_stop = min(new_stop, current_price * 0.99)
                stop = max(stop, new_stop)
            elif side == "short" and (entry - current_price) > activation:
                new_stop = current_price + (atr * atr_mult * 0.7)
                new_stop = max(new_stop, current_price * 1.01)
                stop = min(stop, new_stop)
        
        # МАКСИМАЛЬНЫЙ УБЫТОК
        max_loss_pct = self.max_risk_pct
        if market_regime == "trending":
            max_loss_pct *= 1.2
        elif volatility_ratio > 1.5:
            max_loss_pct *= 0.8
        
        if side == "long":
            max_loss_stop = entry * (1 - max_loss_pct)
            stop = min(stop, max_loss_stop)
        else:
            max_loss_stop = entry * (1 + max_loss_pct)
            stop = max(stop, max_loss_stop)
        
        # ПРОСКАЛЬЗЫВАНИЕ
        if side == "long":
            stop = stop * (1 + self.slippage_pct)
        else:
            stop = stop * (1 - self.slippage_pct)
        
        return round(stop, 2)
    
    def check_exit(self, current_price, stop, side, bar_low=None, bar_high=None, close_price=None):
        if self.exit_mode == "intrabar":
            if side == "long":
                return bar_low <= stop if bar_low is not None else current_price <= stop
            else:
                return bar_high >= stop if bar_high is not None else current_price >= stop
        else:  # close
            price_to_check = close_price if close_price is not None else current_price
            if side == "long":
                return price_to_check <= stop
            else:
                return price_to_check >= stop

    def get_recommended_settings(self, instrument_type):
        recommendations = {
            "crypto": {
                "base_atr_mult": 2.5,
                "min_risk_pct": 0.008,
                "max_risk_pct": 0.06,
                "exit_mode": "intrabar",
                "slippage_pct": 0.002
            }
        }
        return recommendations.get(instrument_type, recommendations["crypto"])