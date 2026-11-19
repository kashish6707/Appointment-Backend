# WhatsApp templates

Create each one in **WhatsApp Manager → Message templates → Create template**:
- Category: **Utility**. Name: exactly as below. Add **two languages**: English and Hindi, same name.
- Body: paste the text exactly (copy the `{{1}}` variables too). No header, footer or buttons.
- Meta asks for a sample value for each variable: use the samples listed.

Generated from `app/notifications.py` by `python -m scripts.whatsapp_templates`. Don't edit by hand.

## `clinically_token_booked`

**English (en)**
```
Hello {{1}}, your token number is {{2}} for {{3}} at {{4}} on {{5}}. OPD starts around {{6}}. Follow the queue live: {{7}} . Clinically
```
**Hindi (hi)**
```
नमस्ते {{1}}, {{4}} में {{3}} के लिए {{5}} को आपका टोकन नंबर {{2}} है। ओपीडी लगभग {{6}} पर शुरू होगी। लाइन लाइव देखें: {{7}} . Clinically
```
Samples: {{1}} = Sunita Devi, {{2}} = 12, {{3}} = Dr. Rajeev Anand, {{4}} = Anand Ortho Care, {{5}} = Thu 20 Nov, {{6}} = 10 am, {{7}} = https://clinically.in/track/abc?code=xyz

## `clinically_opd_started`

**English (en)**
```
Clinically: {{1}} has started OPD at {{2}}. Your token is {{3}}. Patients before you: {{4}}. Follow the queue live: {{5}} . Thank you
```
**Hindi (hi)**
```
Clinically: {{1}} ने {{2}} में ओपीडी शुरू कर दी है। आपका टोकन {{3}} है। आपसे पहले मरीज़: {{4}}। लाइन लाइव देखें: {{5}} . धन्यवाद
```
Samples: {{1}} = Dr. Rajeev Anand, {{2}} = Anand Ortho Care, {{3}} = 12, {{4}} = 5, {{5}} = https://clinically.in/track/abc?code=xyz

## `clinically_doctor_break`

**English (en)**
```
Clinically: {{1}} is on a short break of {{2}} minutes and will be back around {{3}}. Your token is {{4}}. Live queue: {{5}} . Thank you
```
**Hindi (hi)**
```
Clinically: {{1}} {{2}} मिनट के छोटे ब्रेक पर हैं और लगभग {{3}} तक लौटेंगे। आपका टोकन {{4}} है। लाइव लाइन: {{5}} . धन्यवाद
```
Samples: {{1}} = Dr. Rajeev Anand, {{2}} = 15, {{3}} = 11:30 am, {{4}} = 12, {{5}} = https://clinically.in/track/abc?code=xyz

## `clinically_get_ready`

**English (en)**
```
Clinically: Get ready, only {{1}} patients are before your token {{2}} for {{3}} at {{4}}. Please start coming to the clinic. Live queue: {{5}} . Thank you
```
**Hindi (hi)**
```
Clinically: तैयार हो जाइए, {{4}} में {{3}} के लिए आपके टोकन {{2}} से पहले सिर्फ़ {{1}} मरीज़ हैं। कृपया क्लिनिक आना शुरू करें। लाइव लाइन: {{5}} . धन्यवाद
```
Samples: {{1}} = 2, {{2}} = 12, {{3}} = Dr. Rajeev Anand, {{4}} = Anand Ortho Care, {{5}} = https://clinically.in/track/abc?code=xyz

## `clinically_you_are_next`

**English (en)**
```
Clinically: You're next! Token {{1}}, please be ready outside {{2}}'s room at {{3}}. Thank you
```
**Hindi (hi)**
```
Clinically: अगला नंबर आपका है! टोकन {{1}}, कृपया {{3}} में {{2}} के कमरे के बाहर तैयार रहें। धन्यवाद
```
Samples: {{1}} = 12, {{2}} = Dr. Rajeev Anand, {{3}} = Anand Ortho Care

## `clinically_your_turn`

**English (en)**
```
Clinically: It's your turn. Token {{1}}, please go in to see {{2}} now. Thank you
```
**Hindi (hi)**
```
Clinically: आपकी बारी है। टोकन {{1}}, कृपया अभी {{2}} से मिलने अंदर जाएँ। धन्यवाद
```
Samples: {{1}} = 12, {{2}} = Dr. Rajeev Anand

## `clinically_token_cancelled`

**English (en)**
```
Clinically: Sorry, your token {{1}} for {{2}} on {{3}} is cancelled because the doctor is not available. Any online payment will be refunded. Please book another day.
```
**Hindi (hi)**
```
Clinically: क्षमा करें, {{3}} को {{2}} के लिए आपका टोकन {{1}} रद्द हो गया है क्योंकि डॉक्टर उपलब्ध नहीं हैं। ऑनलाइन भुगतान वापस होगा। कृपया दूसरे दिन बुक करें।
```
Samples: {{1}} = 12, {{2}} = Dr. Rajeev Anand, {{3}} = Thu 20 Nov

## `clinically_opd_closed`

**English (en)**
```
Clinically: {{1}} has closed today's OPD at {{2}} before your token {{3}} was called. Please contact the clinic. Sorry for the trouble.
```
**Hindi (hi)**
```
Clinically: {{1}} ने आपका टोकन {{3}} आने से पहले {{2}} में आज की ओपीडी बंद कर दी है। कृपया क्लिनिक से संपर्क करें। असुविधा के लिए क्षमा करें।
```
Samples: {{1}} = Dr. Rajeev Anand, {{2}} = Anand Ortho Care, {{3}} = 12
