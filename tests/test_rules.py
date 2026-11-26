import hashlib,hmac,json,time,unittest
from urllib.parse import urlencode
from rules_feature import verify_init_data,normalize_delta,telegram_pages

class RulesTests(unittest.TestCase):
    def signed(self,age=0):
        token='test-token'
        data={'auth_date':str(int(time.time())-age),'user':json.dumps({'id':123})}
        key=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
        data['hash']=hmac.new(key,'\n'.join(f'{k}={v}' for k,v in sorted(data.items())).encode(),hashlib.sha256).hexdigest()
        return urlencode(data),token
    def test_signature(self):
        raw,token=self.signed();self.assertEqual(verify_init_data(raw,token),123)
        with self.assertRaises(ValueError):verify_init_data(raw+'x',token)
    def test_expiry(self):
        raw,token=self.signed(4000)
        with self.assertRaises(ValueError):verify_init_data(raw,token)
    def test_embeds_forbidden(self):
        with self.assertRaises(ValueError):normalize_delta({'ops':[{'insert':{'image':'x'}}]})
    def test_link_sanitization(self):
        d=normalize_delta({'ops':[{'insert':'x','attributes':{'link':'javascript:alert(1)'}}]})
        self.assertNotIn('attributes',d['ops'][0])
    def test_plain_text(self):
        d=normalize_delta({'ops':[{'insert':'<script>&\n'}]})
        self.assertEqual(telegram_pages(d),['&lt;script&gt;&amp;\n'])
    def test_long_unicode(self):
        pages=telegram_pages(normalize_delta({'ops':[{'insert':'😀'*7000+'\n'}]}))
        self.assertGreater(len(pages),4)
        for p in pages:self.assertLessEqual(len(p.encode('utf-16-le'))//2,3000)
    def test_heading(self):
        d=normalize_delta({'ops':[{'insert':'Заголовок'},{'insert':'\n','attributes':{'header':1}}]})
        self.assertIn('<b>З</b>',telegram_pages(d)[0])
    def test_limit(self):
        with self.assertRaises(ValueError):normalize_delta({'ops':[{'insert':'x'*100001}]})

if __name__=='__main__':unittest.main()
