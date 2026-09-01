# ENTSOE Assignment with Terraform Deployment

## Prerequisites

- [x] Create a free account on GitHub
- [x] Create a free account on AWS (I recommend to set 0$ budget on your account to
  prevent misuse)
- [ ] Create a free account on transparency.entsoe.eu (<https://transparencyplatform.zendesk.com/hc/en-us/articles/12845911031188-How-to-get-security-token>)
- [x] Install Terraform CLI locally

## Task Requirements

### Data Collection System

Develop an AWS Lambda function that scrapes data from the ENTSO-E Transparency Platform
for selected countries and control areas of your choice, with infrastructure deployed
using Terraform.

Download data source via REST API preferably:

`https://iop-transparency.entsoe.eu/generation/forecast/dayAhead?appState=%7B%22sa%22%3A%5B%22CTA%7C10YSK-SEPS-----K%22%5D%2C%22st%22%3A%22CTA%22%2C%22mm%22%3Atrue%2C%22ma%22%3Afalse%2C%22sp%22%3A%22HALF%22%2C%22dt%22%3A%22TABLE%22%2C%22df%22%3A%5B%222026-01-01%22%2C%222026-01-01%22%5D%2C%22tz%22%3A%22CET%22%7D`

Make lambda use NAT gateway to access the internet. Preferable use <https://github.com/AndrewGuenther/fck-nat?tab=readme-ov-file>

### Core Requirements

1. **Data Storage**: Store the scraped data in AWS S3 as CSV files.

2. **Flexible Data Handling**:
   - Implement a generic data processing approach that adapts to changes in the response
     structure
   - Design the solution to be reusable for other endpoints, so I can add scrape new
     data from
     `https://iop-transparency.entsoe.eu/generation/actual/perUnit?appState=%7B%22sa%22%3A%5B%22CTA%7C10YSK-SEPS-----K%22%5D%2C%22st%22%3A%22CTA%22%2C%22mm%22%3Atrue%2C%22ma%22%3Afalse%2C%22sp%22%3A%22HALF%22%2C%22dt%22%3A%22TABLE%22%2C%22df%22%3A%222026-01-01%22%2C%22tz%22%3A%22CET%22%7D`
     just by changing/or adding some configuration

3. **Infrastructure as Code**:
   - Use Terraform to define and provision all required AWS resources:
      - Lambda function
      - IAM roles and permissions
      - S3 bucket
      - CloudWatch\Event bridge events for scheduling (if applicable)
      - AWS NAT or Fck-nat gateway(preferred)
      - Any other necessary resources

4. **Documentation & Code Quality**:
   - Add appropriate comments and documentation
   - Follow best practices for both AWS Lambda and Terraform development
   - Include instructions for deploying the infrastructure

### Submission Process

- Create a GitHub repository containing both application code and Terraform configuration
- Submit your completed assignment as a pull request
- Include a README with setup and deployment instructions

## Additional Information

Feel free to make reasonable assumptions about any aspects not explicitly defined in the
requirements. You may choose any programming language supported by AWS Lambda. Document
any assumptions or design decisions in your submission.
API documentation available here <https://documenter.getpostman.com/view/7009892/2s93JtP3F6#e2e1a56e-2ee1-4b83-b1db-8a3d21cc0ac0>
