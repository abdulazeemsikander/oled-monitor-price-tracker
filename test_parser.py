from bs4 import BeautifulSoup
from src.price_tracker import amazon_price_candidates, price_candidates

html = """
<html><head>
<meta itemprop="price" content="649.99">
<script type="application/ld+json">{"offers":{"price":"649.99","priceCurrency":"EUR"}}</script>
</head>
<body><div class="price">649,99 €</div></body></html>
"""
prices = price_candidates(BeautifulSoup(html, "html.parser"), "test")
assert 649.99 in prices

amazon_html = """
<html><body>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price"><span class="a-price-whole">629</span><span class="a-price-fraction">00</span></span>
  <span class="a-text-price"><span class="a-offscreen">799,00 €</span></span>
</div>
</body></html>
"""
prices = amazon_price_candidates(BeautifulSoup(amazon_html, "html.parser"))
assert prices[0] == 629.0, prices
print("Parser tests OK")
